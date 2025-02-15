#!/usr/bin/env python3
#
# Copyright (C) 2018 VyOS maintainers and contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#

import sys
import os
import shutil
import stat
import pwd
import time

import jinja2
import random
import binascii
import re

import vyos.version
import vyos.validate

from vyos.config import Config
from vyos import ConfigError

config_file_client = r'/etc/snmp/snmp.conf'
config_file_daemon = r'/etc/snmp/snmpd.conf'
config_file_access = r'/usr/share/snmp/snmpd.conf'
config_file_user   = r'/var/lib/snmp/snmpd.conf'
config_file_init   = r'/etc/default/snmpd'

# SNMP OIDs used to mark auth/priv type
OIDs = {
    'md5' : '.1.3.6.1.6.3.10.1.1.2',
    'sha' : '.1.3.6.1.6.3.10.1.1.3',
    'aes' : '.1.3.6.1.6.3.10.1.2.4',
    'des' : '.1.3.6.1.6.3.10.1.2.2',
    'none': '.1.3.6.1.6.3.10.1.2.1'
}
# SNMP template (/etc/snmp/snmp.conf) - be careful if you edit the template.
client_config_tmpl = """
### Autogenerated by snmp.py ###
{% if trap_source -%}
clientaddr {{ trap_source }}
{% endif %}

"""

# SNMP template (/usr/share/snmp/snmpd.conf) - be careful if you edit the template.
access_config_tmpl = """
### Autogenerated by snmp.py ###
{%- for u in v3_users %}
{{ u.mode }}user {{ u.name }}
{%- endfor %}

rwuser {{ vyos_user }}

"""

# SNMP template (/var/lib/snmp/snmpd.conf) - be careful if you edit the template.
user_config_tmpl = """
### Autogenerated by snmp.py ###
# user
{%- for u in v3_users %}
{%- if u.authOID == 'none' %}
createUser {{ u.name }}
{%- elif u.authPassword %}
createUser {{ u.name }} {{ u.authProtocol | upper }} "{{ u.authPassword }}" {{ u.privProtocol | upper }} {{ u.privPassword }}
{%- else %}
usmUser 1 3 {{ u.engineID }} "{{ u.name }}" "{{ u.name }}" NULL {{ u.authOID }} {{ u.authMasterKey }} {{ u.privOID }} {{ u.privMasterKey }} 0x
{%- endif %}
{%- endfor %}

createUser {{ vyos_user }} MD5 "{{ vyos_user_pass }}" DES
{%- if v3_engineid %}
oldEngineID {{ v3_engineid }}
{%- endif %}
"""

# SNMP template (/etc/snmp/snmpd.conf) - be careful if you edit the template.
daemon_config_tmpl = """
### Autogenerated by snmp.py ###

# non configurable defaults
sysObjectID 1.3.6.1.4.1.44641
sysServices 14
master agentx
agentXPerms 0777 0777
pass .1.3.6.1.2.1.31.1.1.1.18 /opt/vyatta/sbin/if-mib-alias
smuxpeer .1.3.6.1.2.1.83
smuxpeer .1.3.6.1.2.1.157
smuxsocket localhost

# linkUp/Down configure the Event MIB tables to monitor
# the ifTable for network interfaces being taken up or down
# for making internal queries to retrieve any necessary information
iquerySecName {{ vyos_user }}

# Modified from the default linkUpDownNotification
# to include more OIDs and poll more frequently
notificationEvent  linkUpTrap    linkUp   ifIndex ifDescr ifType ifAdminStatus ifOperStatus
notificationEvent  linkDownTrap  linkDown ifIndex ifDescr ifType ifAdminStatus ifOperStatus
monitor  -r 10 -e linkUpTrap   "Generate linkUp" ifOperStatus != 2
monitor  -r 10 -e linkDownTrap "Generate linkDown" ifOperStatus == 2

########################
# configurable section #
########################
{% if v3_tsm_key %}
[snmp] localCert {{ v3_tsm_key }}
{%- endif %}

# Default system description is VyOS version
sysDescr VyOS {{ version }}

{% if description %}
# Description
SysDescr {{ description }}
{%- endif %}

# Listen
agentaddress unix:/run/snmpd.socket{% if listen_on %}{% for li in listen_on %},{{ li }}{% endfor %}{% else %},udp:161,udp6:161{% endif %}{% if v3_tsm_key %},tlstcp:{{ v3_tsm_port }},dtlsudp::{{ v3_tsm_port }}{% endif %}

# SNMP communities
{%- for c in communities %}

{%- if c.network_v4 %}
{%- for network in c.network_v4 %}
{{ c.authorization }}community {{ c.name }} {{ network }}
{%- endfor %}
{%- elif not c.has_source %}
{{ c.authorization }}community {{ c.name }}
{%- endif %}

{%- if c.network_v6 %}
{%- for network in c.network_v6 %}
{{ c.authorization }}community6 {{ c.name }} {{ network }}
{%- endfor %}
{%- elif not c.has_source %}
{{ c.authorization }}community6 {{ c.name }}
{%- endif %}

{%- endfor %}

{% if contact %}
# system contact information
SysContact {{ contact }}
{%- endif %}

{% if location %}
# system location information
SysLocation {{ location }}
{%- endif %}

{% if smux_peers -%}
# additional smux peers
{%- for sp in smux_peers %}
smuxpeer {{ sp }}
{%- endfor %}
{%- endif %}

{% if trap_targets -%}
# if there is a problem - tell someone!
{%- for t in trap_targets %}
trap2sink {{ t.target }}{% if t.port -%}:{{ t.port }}{% endif %} {{ t.community }}
{%- endfor %}
{%- endif %}

{%- if v3_enabled %}
#
# SNMPv3 stuff goes here
#
# views
{%- for v in v3_views %}
{%- for oid in v.oids %}
view {{ v.name }} included .{{ oid.oid }}
{%- endfor %}
{%- endfor %}

# access
#             context sec.model sec.level match  read    write  notif
{%- for g in v3_groups %}
access {{ g.name }} "" usm {{ g.seclevel }} exact {{ g.view }} {% if g.mode == 'ro' %}none{% else %}{{ g.view }}{% endif %} none
access {{ g.name }} "" tsm {{ g.seclevel }} exact {{ g.view }} {% if g.mode == 'ro' %}none{% else %}{{ g.view }}{% endif %} none
{%- endfor %}

# trap-target
{%- for t in v3_traps %}
trapsess -v 3 {{ '-Ci' if t.type == 'inform' }} -e {{ t.engineID }} -u {{ t.secName }} -l {{ t.secLevel }} -a {{ t.authProtocol }} {% if t.authPassword %}-A {{ t.authPassword }}{% elif t.authMasterKey %}-3m {{ t.authMasterKey }}{% endif %} -x {{ t.privProtocol }} {% if t.privPassword %}-X {{ t.privPassword }}{% elif t.privMasterKey %}-3M {{ t.privMasterKey }}{% endif %} {{ t.ipProto }}:{{ t.ipAddr }}:{{ t.ipPort }}
{%- endfor %}

# group
{%- for u in v3_users %}
group {{ u.group }} usm {{ u.name }}
group {{ u.group }} tsm {{ u.name }}
{% endfor %}
{%- endif %}

{% if script_ext %}
# extension scripts
{%- for ext in script_ext|sort %}
extend\t{{ext}}\t{{script_ext[ext]}}
{%- endfor %}
{% endif %}
"""

# SNMP template (/etc/default/snmpd) - be careful if you edit the template.
init_config_tmpl = """
### Autogenerated by snmp.py ###
# This file controls the activity of snmpd

# snmpd control (yes means start daemon).
SNMPDRUN=yes

# snmpd options (use syslog, close stdin/out/err).
SNMPDOPTS='-LSed -u snmp -g snmp -I -ipCidrRouteTable,inetCidrRouteTable -p /run/snmpd.pid'
"""

default_config_data = {
    'listen_on': [],
    'listen_address': [],
    'communities': [],
    'smux_peers': [],
    'location' : '',
    'description' : '',
    'contact' : '',
    'trap_source': '',
    'trap_targets': [],
    'vyos_user': '',
    'vyos_user_pass': '',
    'version': '999',
    'v3_enabled': 'False',
    'v3_engineid': '',
    'v3_groups': [],
    'v3_traps': [],
    'v3_tsm_key': '',
    'v3_tsm_port': '10161',
    'v3_users': [],
    'v3_views': [],
    'script_ext': {}
}

def rmfile(file):
    if os.path.isfile(file):
        os.unlink(file)

def get_config():
    snmp = default_config_data
    conf = Config()
    if not conf.exists('service snmp'):
        return None
    else:
        conf.set_level('service snmp')

    version_data = vyos.version.get_version_data()
    snmp['version'] = version_data['version']

    # create an internal snmpv3 user of the form 'vyattaxxxxxxxxxxxxxxxx'
    # os.urandom(8) returns 8 bytes of random data
    snmp['vyos_user'] = 'vyatta' + binascii.hexlify(os.urandom(8)).decode('utf-8')
    snmp['vyos_user_pass'] = binascii.hexlify(os.urandom(16)).decode('utf-8')

    if conf.exists('community'):
        for name in conf.list_nodes('community'):
            community = {
                'name': name,
                'authorization': 'ro',
                'network_v4': [],
                'network_v6': [],
                'has_source' : False
            }

            if conf.exists('community {0} authorization'.format(name)):
                community['authorization'] = conf.return_value('community {0} authorization'.format(name))

            # Subnet of SNMP client(s) allowed to contact system
            if conf.exists('community {0} network'.format(name)):
                for addr in conf.return_values('community {0} network'.format(name)):
                    if vyos.validate.is_ipv4(addr):
                        community['network_v4'].append(addr)
                    else:
                        community['network_v6'].append(addr)

            # IP address of SNMP client allowed to contact system
            if conf.exists('community {0} client'.format(name)):
                for addr in conf.return_values('community {0} client'.format(name)):
                    if vyos.validate.is_ipv4(addr):
                        community['network_v4'].append(addr)
                    else:
                        community['network_v6'].append(addr)

            if (len(community['network_v4']) > 0) or (len(community['network_v6']) > 0):
                 community['has_source'] = True

            snmp['communities'].append(community)

    if conf.exists('contact'):
        snmp['contact'] = conf.return_value('contact')

    if conf.exists('description'):
        snmp['description'] = conf.return_value('description')

    if conf.exists('listen-address'):
        for addr in conf.list_nodes('listen-address'):
            port = '161'
            if conf.exists('listen-address {0} port'.format(addr)):
                port = conf.return_value('listen-address {0} port'.format(addr))

            snmp['listen_address'].append((addr, port))

        # Always listen on localhost if an explicit address has been configured
        # This is a safety measure to not end up with invalid listen addresses
        # that are not configured on this system. See https://phabricator.vyos.net/T850
        if not '127.0.0.1' in conf.list_nodes('listen-address'):
            snmp['listen_address'].append(('127.0.0.1', '161'))

        if not '::1' in conf.list_nodes('listen-address'):
            snmp['listen_address'].append(('::1', '161'))

    if conf.exists('location'):
        snmp['location'] = conf.return_value('location')

    if conf.exists('smux-peer'):
        snmp['smux_peers'] = conf.return_values('smux-peer')

    if conf.exists('trap-source'):
        snmp['trap_source'] = conf.return_value('trap-source')

    if conf.exists('trap-target'):
        for target in conf.list_nodes('trap-target'):
            trap_tgt = {
                'target': target,
                'community': '',
                'port': ''
            }

            if conf.exists('trap-target {0} community'.format(target)):
               trap_tgt['community'] = conf.return_value('trap-target {0} community'.format(target))

            if conf.exists('trap-target {0} port'.format(target)):
                trap_tgt['port'] = conf.return_value('trap-target {0} port'.format(target))

            snmp['trap_targets'].append(trap_tgt)

    #
    # 'set service snmp script-extensions'
    #
    if conf.exists('script-extensions'):
      for extname in conf.list_nodes('script-extensions extension-name'):
        snmp['script_ext'][extname] = '/config/user-data/' + conf.return_value('script-extensions extension-name ' + extname + ' script')


    #########################################################################
    #                ____  _   _ __  __ ____          _____                 #
    #               / ___|| \ | |  \/  |  _ \  __   _|___ /                 #
    #               \___ \|  \| | |\/| | |_) | \ \ / / |_ \                 #
    #                ___) | |\  | |  | |  __/   \ V / ___) |                #
    #               |____/|_| \_|_|  |_|_|       \_/ |____/                 #
    #                                                                       #
    #     now take care about the fancy SNMP v3 stuff, or bail out eraly    #
    #########################################################################
    if not conf.exists('v3'):
        return snmp
    else:
        snmp['v3_enabled'] = True

    #
    # 'set service snmp v3 engineid'
    #
    if conf.exists('v3 engineid'):
        snmp['v3_engineid'] = conf.return_value('v3 engineid')

    #
    # 'set service snmp v3 group'
    #
    if conf.exists('v3 group'):
        for group in conf.list_nodes('v3 group'):
            v3_group = {
                'name': group,
                'mode': 'ro',
                'seclevel': 'auth',
                'view': ''
            }

            if conf.exists('v3 group {0} mode'.format(group)):
                v3_group['mode'] = conf.return_value('v3 group {0} mode'.format(group))

            if conf.exists('v3 group {0} seclevel'.format(group)):
                v3_group['seclevel'] = conf.return_value('v3 group {0} seclevel'.format(group))

            if conf.exists('v3 group {0} view'.format(group)):
                v3_group['view'] = conf.return_value('v3 group {0} view'.format(group))

            snmp['v3_groups'].append(v3_group)

    #
    # 'set service snmp v3 trap-target'
    #
    if conf.exists('v3 trap-target'):
        for trap in conf.list_nodes('v3 trap-target'):
            trap_cfg = {
                'ipAddr': trap,
                'engineID': '',
                'secName': '',
                'authProtocol': 'md5',
                'authPassword': '',
                'authMasterKey': '',
                'privProtocol': 'des',
                'privPassword': '',
                'privMasterKey': '',
                'ipProto': 'udp',
                'ipPort': '162',
                'type': '',
                'secLevel': 'noAuthNoPriv'
            }

            if conf.exists('v3 trap-target {0} engineid'.format(trap)):
                # Set the context engineID used for SNMPv3 REQUEST messages scopedPdu.
                # If not specified, this will default to the authoritative engineID.
                trap_cfg['engineID'] = conf.return_value('v3 trap-target {0} engineid'.format(trap))

            if conf.exists('v3 trap-target {0} user'.format(trap)):
                # Set the securityName used for authenticated SNMPv3 messages.
                trap_cfg['secName'] = conf.return_value('v3 trap-target {0} user'.format(trap))

            if conf.exists('v3 trap-target {0} auth type'.format(trap)):
                # Set the authentication protocol (MD5 or SHA) used for authenticated SNMPv3 messages
                # cmdline option '-a'
                trap_cfg['authProtocol'] = conf.return_value('v3 trap-target {0} auth type'.format(trap))

            if conf.exists('v3 trap-target {0} auth plaintext-key'.format(trap)):
                # Set the authentication pass phrase used for authenticated SNMPv3 messages.
                # cmdline option '-A'
                trap_cfg['authPassword'] = conf.return_value('v3 trap-target {0} auth plaintext-key'.format(trap))

            if conf.exists('v3 trap-target {0} auth encrypted-key'.format(trap)):
                # Sets the keys to be used for SNMPv3 transactions. These options allow you to set the master authentication keys.
                # cmdline option '-3m'
                trap_cfg['authMasterKey'] = conf.return_value('v3 trap-target {0} auth encrypted-key'.format(trap))

            if conf.exists('v3 trap-target {0} privacy type'.format(trap)):
                # Set the privacy protocol (DES or AES) used for encrypted SNMPv3 messages.
                # cmdline option '-x'
                trap_cfg['privProtocol'] = conf.return_value('v3 trap-target {0} privacy type'.format(trap))

            if conf.exists('v3 trap-target {0} privacy plaintext-key'.format(trap)):
                # Set the privacy pass phrase used for encrypted SNMPv3 messages.
                # cmdline option '-X'
                trap_cfg['privPassword'] = conf.return_value('v3 trap-target {0} privacy plaintext-key'.format(trap))

            if conf.exists('v3 trap-target {0} privacy encrypted-key'.format(trap)):
                # Sets the keys to be used for SNMPv3 transactions. These options allow you to set the master encryption keys.
                # cmdline option '-3M'
                trap_cfg['privMasterKey'] = conf.return_value('v3 trap-target {0} privacy encrypted-key'.format(trap))

            if conf.exists('v3 trap-target {0} protocol'.format(trap)):
                trap_cfg['ipProto'] = conf.return_value('v3 trap-target {0} protocol'.format(trap))

            if conf.exists('v3 trap-target {0} port'.format(trap)):
                trap_cfg['ipPort'] = conf.return_value('v3 trap-target {0} port'.format(trap))

            if conf.exists('v3 trap-target {0} type'.format(trap)):
                trap_cfg['type'] = conf.return_value('v3 trap-target {0} type'.format(trap))

            # Determine securityLevel used for SNMPv3 messages (noAuthNoPriv|authNoPriv|authPriv).
            # Appropriate pass phrase(s) must provided when using any level higher than noAuthNoPriv.
            if trap_cfg['authPassword'] or trap_cfg['authMasterKey']:
                if trap_cfg['privProtocol'] or trap_cfg['privPassword']:
                    trap_cfg['secLevel'] = 'authPriv'
                else:
                    trap_cfg['secLevel'] = 'authNoPriv'

            snmp['v3_traps'].append(trap_cfg)

    #
    # 'set service snmp v3 tsm'
    #
    if conf.exists('v3 tsm'):
        if conf.exists('v3 tsm local-key'):
            snmp['v3_tsm_key'] = conf.return_value('v3 tsm local-key')

        if conf.exists('v3 tsm port'):
            snmp['v3_tsm_port'] = conf.return_value('v3 tsm port')

    #
    # 'set service snmp v3 user'
    #
    if conf.exists('v3 user'):
        for user in conf.list_nodes('v3 user'):
            user_cfg = {
                'name': user,
                'authMasterKey': '',
                'authPassword': '',
                'authProtocol': 'md5',
                'authOID': 'none',
                'engineID': '',
                'group': '',
                'mode': 'ro',
                'privMasterKey': '',
                'privPassword': '',
                'privOID': '',
                'privTsmKey': '',
                'privProtocol': 'des'
            }

            #
            # v3 user {0} auth
            #
            if conf.exists('v3 user {0} auth encrypted-key'.format(user)):
                user_cfg['authMasterKey'] = conf.return_value('v3 user {0} auth encrypted-key'.format(user))

            if conf.exists('v3 user {0} auth plaintext-key'.format(user)):
                user_cfg['authPassword'] = conf.return_value('v3 user {0} auth plaintext-key'.format(user))

            # load default value
            type = user_cfg['authProtocol']
            if conf.exists('v3 user {0} auth type'.format(user)):
                type = conf.return_value('v3 user {0} auth type'.format(user))

            # (re-)update with either default value or value from CLI
            user_cfg['authProtocol'] = type
            user_cfg['authOID'] = OIDs[type]

            #
            # v3 user {0} engineid
            #
            if conf.exists('v3 user {0} engineid'.format(user)):
                user_cfg['engineID'] = conf.return_value('v3 user {0} engineid'.format(user))

            #
            # v3 user {0} group
            #
            if conf.exists('v3 user {0} group'.format(user)):
                user_cfg['group'] = conf.return_value('v3 user {0} group'.format(user))

            #
            # v3 user {0} mode
            #
            if conf.exists('v3 user {0} mode'.format(user)):
                user_cfg['mode'] = conf.return_value('v3 user {0} mode'.format(user))

            #
            # v3 user {0} privacy
            #
            if conf.exists('v3 user {0} privacy encrypted-key'.format(user)):
                user_cfg['privMasterKey'] = conf.return_value('v3 user {0} privacy encrypted-key'.format(user))

            if conf.exists('v3 user {0} privacy plaintext-key'.format(user)):
                user_cfg['privPassword'] = conf.return_value('v3 user {0} privacy plaintext-key'.format(user))

            if conf.exists('v3 user {0} privacy tsm-key'.format(user)):
                user_cfg['privTsmKey'] = conf.return_value('v3 user {0} privacy tsm-key'.format(user))

            # load default value
            type = user_cfg['privProtocol']
            if conf.exists('v3 user {0} privacy type'.format(user)):
                type = conf.return_value('v3 user {0} privacy type'.format(user))

            # (re-)update with either default value or value from CLI
            user_cfg['privProtocol'] = type
            user_cfg['privOID'] = OIDs[type]

            snmp['v3_users'].append(user_cfg)

    #
    # 'set service snmp v3 view'
    #
    if conf.exists('v3 view'):
        for view in conf.list_nodes('v3 view'):
            view_cfg = {
                'name': view,
                'oids': []
            }

            if conf.exists('v3 view {0} oid'.format(view)):
                for oid in conf.list_nodes('v3 view {0} oid'.format(view)):
                    oid_cfg = {
                        'oid': oid
                    }
                    view_cfg['oids'].append(oid_cfg)
            snmp['v3_views'].append(view_cfg)

    return snmp

def verify(snmp):
    if snmp is None:
        return None

    ### check if the configured script actually exist under /config/user-data
    if snmp['script_ext']:
      for ext in snmp['script_ext']:
        if not os.path.isfile(snmp['script_ext'][ext]):
          print ("WARNING: script: " + snmp['script_ext'][ext] + " doesn\'t exist")  
        else:
          os.chmod(snmp['script_ext'][ext], 0o555)

    # bail out early if SNMP v3 is not configured
    if not snmp['v3_enabled']:
        return None

    tsmKeyPattern = re.compile('^[0-9A-F]{2}(:[0-9A-F]{2}){19}$', re.IGNORECASE)

    if snmp['v3_tsm_key']:
        if not tsmKeyPattern.match(snmp['v3_tsm_key']):
            if not os.path.isfile('/etc/snmp/tls/certs/' + snmp['v3_tsm_key']):
                if not os.path.isfile('/config/snmp/tls/certs/' + snmp['v3_tsm_key']):
                    raise ConfigError('TSM key must be fingerprint or filename in "/config/snmp/tls/certs/" folder')

    for listen in snmp['listen_address']:
        addr = listen[0]
        port = listen[1]

        if vyos.validate.is_ipv4(addr):
            # example: udp:127.0.0.1:161
            listen = 'udp:' + addr + ':' + port
        else:
            # example: udp6:[::1]:161
            listen = 'udp6:' + '[' + addr + ']' + ':' + port

        # We only wan't to configure addresses that exist on the system.
        # Hint the user if they don't exist
        if vyos.validate.is_addr_assigned(addr):
            snmp['listen_on'].append(listen)
        else:
            print('WARNING: SNMP listen address {0} not configured!'.format(addr))

    if 'v3_groups' in snmp.keys():
        for group in snmp['v3_groups']:
            #
            # A view must exist prior to mapping it into a group
            #
            if 'view' in group.keys():
                error = True
                if 'v3_views' in snmp.keys():
                    for view in snmp['v3_views']:
                        if view['name'] == group['view']:
                            error = False
                if error:
                    raise ConfigError('You must create view "{0}" first'.format(group['view']))
            else:
                raise ConfigError('"view" must be specified')

            if not 'mode' in group.keys():
                raise ConfigError('"mode" must be specified')

            if not 'seclevel' in group.keys():
                raise ConfigError('"seclevel" must be specified')

    if 'v3_traps' in snmp.keys():
        for trap in snmp['v3_traps']:
            if trap['authPassword'] and trap['authMasterKey']:
                raise ConfigError('Must specify only one of encrypted-key/plaintext-key for trap auth')

            if trap['authPassword'] == '' and trap['authMasterKey'] == '':
                raise ConfigError('Must specify encrypted-key or plaintext-key for trap auth')

            if trap['privPassword'] and trap['privMasterKey']:
                raise ConfigError('Must specify only one of encrypted-key/plaintext-key for trap privacy')

            if trap['privPassword'] == '' and trap['privMasterKey'] == '':
                raise ConfigError('Must specify encrypted-key or plaintext-key for trap privacy')

            if not 'type' in trap.keys():
                raise ConfigError('v3 trap: "type" must be specified')

            if not 'authPassword' and 'authMasterKey' in trap.keys():
                raise ConfigError('v3 trap: "auth" must be specified')

            if not 'authProtocol' in trap.keys():
                raise ConfigError('v3 trap: "protocol" must be specified')

            if not 'privPassword' and 'privMasterKey' in trap.keys():
                raise ConfigError('v3 trap: "user" must be specified')

            if 'type' in trap.keys():
                if trap['type'] == 'trap' and trap['engineID'] == '':
                    raise ConfigError('must specify engineid if type is "trap"')
            else:
                raise ConfigError('"type" must be specified')


    if 'v3_users' in snmp.keys():
        for user in snmp['v3_users']:
            #
            # Group must exist prior to mapping it into a group
            # seclevel will be extracted from group
            #
            if user['group']:
                error = True
                if 'v3_groups' in snmp.keys():
                    for group in snmp['v3_groups']:
                        if group['name'] == user['group']:
                            seclevel = group['seclevel']
                            error = False

                if error:
                    raise ConfigError('You must create group "{0}" first'.format(user['group']))

            # Depending on the configured security level
            # the user has to provide additional info
            if user['authPassword'] and user['authMasterKey']:
                raise ConfigError('Can not mix "encrypted-key" and "plaintext-key" for user auth')

            if (not user['authPassword'] and not user['authMasterKey']):
                raise ConfigError('Must specify encrypted-key or plaintext-key for user auth')

            if user['privPassword'] and user['privMasterKey']:
                raise ConfigError('Can not mix "encrypted-key" and "plaintext-key" for user privacy')

            if user['privPassword'] == '' and user['privMasterKey'] == '':
                raise ConfigError('Must specify encrypted-key or plaintext-key for user privacy')

            if user['privMasterKey'] and user['engineID'] == '':
                raise ConfigError('Can not have "encrypted-key" without engineid')

            if user['authPassword'] == '' and user['authMasterKey'] == '' and user['privTsmKey'] == '':
                raise ConfigError('Must specify auth or tsm-key for user auth')

            if user['mode'] == '':
                raise ConfigError('Must specify user mode ro/rw')

            if user['privTsmKey']:
                if not tsmKeyPattern.match(snmp['v3_tsm_key']):
                    if not os.path.isfile('/etc/snmp/tls/certs/' + snmp['v3_tsm_key']):
                        if not os.path.isfile('/config/snmp/tls/certs/' + snmp['v3_tsm_key']):
                            raise ConfigError('User TSM key must be fingerprint or filename in "/config/snmp/tls/certs/" folder')

    if 'v3_views' in snmp.keys():
        for view in snmp['v3_views']:
            if not view['oids']:
                raise ConfigError('Must configure an oid')

    return None

def generate(snmp):
    #
    # As we are manipulating the snmpd user database we have to stop it first!
    # This is even save if service is going to be removed
    os.system("sudo systemctl stop snmpd.service")
    rmfile(config_file_client)
    rmfile(config_file_daemon)
    rmfile(config_file_access)
    rmfile(config_file_user)

    if snmp is None:
        return None

    # Write client config file
    tmpl = jinja2.Template(client_config_tmpl)
    config_text = tmpl.render(snmp)
    with open(config_file_client, 'w') as f:
        f.write(config_text)

    # Write server config file
    tmpl = jinja2.Template(daemon_config_tmpl)
    config_text = tmpl.render(snmp)
    with open(config_file_daemon, 'w') as f:
        f.write(config_text)

    # Write access rights config file
    tmpl = jinja2.Template(access_config_tmpl)
    config_text = tmpl.render(snmp)
    with open(config_file_access, 'w') as f:
        f.write(config_text)

    # Write access rights config file
    tmpl = jinja2.Template(user_config_tmpl)
    config_text = tmpl.render(snmp)
    with open(config_file_user, 'w') as f:
        f.write(config_text)

    # Write init config file
    tmpl = jinja2.Template(init_config_tmpl)
    config_text = tmpl.render(snmp)
    with open(config_file_init, 'w') as f:
        f.write(config_text)

    return None

def apply(snmp):
    if snmp is not None:

        nonvolatiledir = '/config/snmp/tls'
        volatiledir = '/etc/snmp/tls'
        if not os.path.exists(nonvolatiledir):
            os.makedirs(nonvolatiledir)
            os.chmod(nonvolatiledir, stat.S_IWUSR | stat.S_IRUSR)
            # get uid for user 'snmp'
            snmp_uid = pwd.getpwnam('snmp').pw_uid
            os.chown(nonvolatiledir, snmp_uid, -1)

            # move SNMP certificate files from volatile location to non volatile /config/snmp
            if os.path.exists(volatiledir) and os.path.isdir(volatiledir):
                files = os.listdir(volatiledir)
                for f in files:
                    shutil.move(volatiledir + '/' + f, nonvolatiledir)
                    os.chmod(nonvolatiledir + '/' + f, stat.S_IWUSR | stat.S_IRUSR)

                os.rmdir(volatiledir)
                os.symlink(nonvolatiledir, volatiledir)

        if os.path.islink(volatiledir):
            link = os.readlink(volatiledir)
            if link != nonvolatiledir:
                os.unlink(volatiledir)
                os.symlink(nonvolatiledir, volatiledir)

        # start SNMP daemon
        os.system("sudo systemctl restart snmpd.service")

        # Passwords are not available immediately in the configuration file,
        # after daemon startup - we wait until they have been processed by
        # snmpd, which we see when a magic line appears in this file.
        snmpReady = False
        while not snmpReady:
            while not os.path.exists(config_file_user):
                time.sleep(1)

            with open(config_file_user, 'r') as f:
                for line in f:
                    # Search for our magic string inside the file
                    if '**** DO NOT EDIT THIS FILE ****' in line:
                        snmpReady = True
                        break

        # Back in the Perl days the configuration was re-read and any
        # plaintext password inside the configuration was replaced by
        # the encrypted one which can be found in 'config_file_user'
        with open(config_file_user, 'r') as f:
            engineID = ''
            for line in f:
                if line.startswith('oldEngineID'):
                    string = line.split(' ')
                    engineID = string[1]

                if line.startswith('usmUser'):
                    string = line.split(' ')
                    cfg = {
                        'user': string[4].replace(r'"', ''),
                        'auth_pw': string[8],
                        'priv_pw': string[10]
                    }
                    # No need to take care about the VyOS internal user
                    if cfg['user'] == snmp['vyos_user']:
                        continue

                    # Now update the running configuration
                    #
                    # Currently when executing os.system() the environment does not have the vyos_libexec_dir variable set, see T685
                    os.system('vyos_libexec_dir=/usr/libexec/vyos /opt/vyatta/sbin/my_set service snmp v3 user "{0}" engineid {1} > /dev/null'.format(cfg['user'], engineID))
                    os.system('vyos_libexec_dir=/usr/libexec/vyos /opt/vyatta/sbin/my_set service snmp v3 user "{0}" auth encrypted-key {1} > /dev/null'.format(cfg['user'], cfg['auth_pw']))
                    os.system('vyos_libexec_dir=/usr/libexec/vyos /opt/vyatta/sbin/my_set service snmp v3 user "{0}" privacy encrypted-key {1} > /dev/null'.format(cfg['user'], cfg['priv_pw']))
                    os.system('vyos_libexec_dir=/usr/libexec/vyos /opt/vyatta/sbin/my_delete service snmp v3 user "{0}" auth plaintext-key > /dev/null'.format(cfg['user']))
                    os.system('vyos_libexec_dir=/usr/libexec/vyos /opt/vyatta/sbin/my_delete service snmp v3 user "{0}" privacy plaintext-key > /dev/null'.format(cfg['user']))

        # Enable AgentX in FRR
        os.system('vtysh -c "configure terminal" -c "agentx" >/dev/null')

    return None

if __name__ == '__main__':
    try:
        c = get_config()
        verify(c)
        generate(c)
        apply(c)
    except ConfigError as e:
        print(e)
        sys.exit(1)
