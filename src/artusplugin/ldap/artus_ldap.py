#!/usr/bin/python
# -*- coding: utf-8 -*-

# Standard lib
import ldap3 as ldap
import syslog
import json

# LDAP server data
# import ARTUS_ldap_data

data = {}
data["main_server"] = "vm03.artus.dom"
data["secondary_server"] = "srv05.artus.dom"
data["bind_username"] = "CN=buildbot, OU=Comptes de service,OU=Comptes ARTUS,DC=artus,DC=dom"
data["bind_password"] = "qFbdmZJrshQ7oDEc"
data["basedn"] = "OU=Utilisateurs,OU=Comptes ARTUS,DC=artus,DC=dom"

ARTUS_ldap_data = json.dumps(data) 

class Artus_Ldap(object):
    """ LDAP functions - see http://www.grotan.com/ldap/python-ldap-samples.html
    See also https://www.python-ldap.org/en/latest/reference/ldap.html
    first you must open a connection to the server
    """

    def __enter__(self):

        try:
            self.l_ARTUS = ldap.initialize("ldap://%s:389,ldap://%s:389" % (ARTUS_ldap_data.main_server, ARTUS_ldap_data.secondary_server))
            # searching doesn't require a bind in LDAP V3.
            # If you're using LDAP v2, set the next line appropriately
            # and do a bind as shown in the above example.
            # you can also set this to ldap.VERSION2 if you're using a v2 directory
            # you should  set the next option to ldap.VERSION2
            # if you're using a v2 directory
            self.l_ARTUS.protocol_version = ldap.VERSION3

            # Pass in a valid username and password to get
            # privileged directory access.
            # If you leave them as empty strings or pass an invalid value
            # you will still bind to the server but with limited privileges.
            # username = "ARTUS\buildbot"
            # Any errors will throw an ldap.LDAPError exception
            # or related exception so you can ignore the result
            self.l_ARTUS.simple_bind(ARTUS_ldap_data.bind_username,
                                     ARTUS_ldap_data.bind_password)
            self.l_ARTUS.result()
        except ldap.LDAPError as e:
            # handle error however you like
            self.l_ARTUS.unbind()
            syslog.syslog(e)
            raise

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.l_ARTUS.unbind()

    @staticmethod
    def ldap_search(ldap_object, baseDN, searchFilter, attrList):
        searchScope = ldap.SCOPE_SUBTREE
        try:
            # Asynchronous search
            ldap_result_id = ldap_object.search(baseDN, searchScope, searchFilter, attrList)
            # Wait for all results
            return ldap_object.result(ldap_result_id, all=1)
        except ldap.LDAPError as e:
            ldap_object.unbind()
            raise

    def exist_in_ARTUS_AD(self, userid):
        ldap_object = self.l_ARTUS
        baseDN = ARTUS_ldap_data.basedn
        searchFilter = "(sAMAccountName=%s)" % userid
        result_type, result_data = self.ldap_search(ldap_object, baseDN, searchFilter, ["mail"])
        if not result_data:
            return False
        else:
            return True

    def get_artus_mail(self, userid):
        ldap_object = self.l_ARTUS
        baseDN = ARTUS_ldap_data.basedn
        searchFilter = "(sAMAccountName=%s)" % userid
        result_type, result_data = self.ldap_search(ldap_object, baseDN, searchFilter, ["mail"])
        if result_data:
           dn, attrs = result_data[0]
           return attrs['mail'][0]
        else:
            return None
