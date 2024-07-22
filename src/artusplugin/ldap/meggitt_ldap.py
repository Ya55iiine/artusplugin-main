#!/usr/bin/python
# -*- coding: utf-8 -*-

# Standard lib
import ldap3 as ldap
import syslog
import json

# LDAP server data
# import MEGGITT_ldap_data

data = {}
data["server_name"] = "MGANDAD01.meggitt.net"
data["bind_username"] = "CN=LDAP\, Avrillé,OU=Resources,OU=Artus Avrille,OU=Business Units,DC=meggitt,DC=net"
data["bind_password"] = "Avion2Plane$"
data["artus_users"] = "OU=Users,OU=Artus Avrille,OU=Business Units,DC=meggitt,DC=net"
data["artus_external_users"] = "OU=External,OU=Users,OU=Artus Avrille,OU=Business Units,DC=meggitt,DC=net"

MEGGITT_ldap_data = json.dumps(data) 

class Meggitt_Ldap(object):
    """ LDAP functions - see http://www.grotan.com/ldap/python-ldap-samples.html
    See also https://www.python-ldap.org/en/latest/reference/ldap.html
    first you must open a connection to the server
    """

    def __enter__(self):
        try:
            self.l_MEGGITT = ldap.initialize("ldap://%s:389" % MEGGITT_ldap_data.server_name)
            # searching doesn't require a bind in LDAP V3.
            # If you're using LDAP v2, set the next line appropriately
            # and do a bind as shown in the above example.
            # you can also set this to ldap.VERSION2 if you're using a v2 directory
            # you should  set the next option to ldap.VERSION2
            # if you're using a v2 directory
            self.l_MEGGITT.protocol_version = ldap.VERSION3

            # Pass in a valid username and password to get
            # privileged directory access.
            # If you leave them as empty strings or pass an invalid value
            # you will still bind to the server but with limited privileges.
            # username = "ARTUS\buildbot"
            # Any errors will throw an ldap.LDAPError exception
            # or related exception so you can ignore the result
            self.l_MEGGITT.simple_bind(MEGGITT_ldap_data.bind_username, MEGGITT_ldap_data.bind_password)
            self.l_MEGGITT.result()
        except ldap.LDAPError as e:
            # handle error however you like
            self.l_MEGGITT.unbind()
            syslog.syslog(e)
            raise

        return self
    
    def __exit__(self, exc_type, exc_value, traceback):
        self.l_MEGGITT.unbind()

    @staticmethod
    def ldap_search(ldap_object, baseDN, searchFilter, attrList):
        searchScope = ldap.SCOPE_ONELEVEL
        try:
            # Asynchronous search
            ldap_result_id = ldap_object.search(baseDN, searchScope, searchFilter,
                                                attrList)
            # Wait for all results
            result_type, result_data = ldap_object.result(ldap_result_id, all=1)
            return result_data
        except ldap.LDAPError as e:
            ldap_object.unbind()
            raise

    def exist_in_MEGGITT_AD(self, userid):
        ldap_object = self.l_MEGGITT
        searchFilter = ("(&(objectClass=user)"
                        "(|(mailNickname=%s)"
                        "(mailNickname=%s.external))"
                        ")" % (userid, userid))
        for baseDN in (MEGGITT_ldap_data.artus_users,
                       MEGGITT_ldap_data.artus_external_users):
            if self.ldap_search(ldap_object, baseDN, searchFilter, ["mail"]):
                return True
            else:
                continue
        else:
            return None
    def get_meggitt_mail(self, userid):
        ldap_object = self.l_MEGGITT
        searchFilter = ("(&(objectClass=user)"
                        "(|(mailNickname=%s)"
                        "(mailNickname=%s.external))"
                        ")" % (userid, userid))
        for baseDN in (MEGGITT_ldap_data.artus_users,
                       MEGGITT_ldap_data.artus_external_users):
            result_set = self.ldap_search(ldap_object, baseDN, searchFilter, ["mail"])
            if result_set:
                dn, attrs = result_set[0]
                return attrs['mail'][0]
            else:
                continue
        else:
            return None

    def get_ldap_displayname(self, userid):
        ldap_object = self.l_MEGGITT
        searchFilter = ("(&(objectClass=user)"
                        "(|(mailNickname=%s)"
                        "(mailNickname=%s.external))"
                        ")" % (userid, userid))
        for baseDN in (MEGGITT_ldap_data.artus_users,
                       MEGGITT_ldap_data.artus_external_users):
            result_set = self.ldap_search(ldap_object, baseDN, searchFilter, ["displayName"])
            if result_set:
                dn, attrs = result_set[0]
                return attrs['displayName'][0]
            else:
                continue
        else:
            return None
