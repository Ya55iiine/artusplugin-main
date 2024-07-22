#!/usr/bin/python
# -*- coding: utf-8 -*-

# Standard lib
# import ConfigParser
from backports import configparser as ConfigParser

# MEGGITT and ARTUS LDAP
from artusplugin.meggitt_ldap import Meggitt_Ldap
from artusplugin.artus_ldap import Artus_Ldap

class Ldap_Utilities(object):

    def __init__(self):
        # email conversion
        self.MEGGITT_TRANSLATION = '/srv/svn/access_right/meggitt-translation.conf'

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass

    def get_meggitt_id(self, userid):
        if '.' in userid:
            # Already a MEGGITT user id
            return userid
        try:
            meggitt_translation = ConfigParser.ConfigParser()
            meggitt_translation.read(self.MEGGITT_TRANSLATION)
            return meggitt_translation.get(
                'user-translation', userid)
        except ConfigParser.NoOptionError:
            return None
    def user_exists(self, userid):
        with Meggitt_Ldap() as mgt_ldap:
            with Artus_Ldap() as art_ldap:
                if '.' in userid:
                    # forename.name
                    return mgt_ldap.exist_in_MEGGITT_AD(userid)
                else:
                    # fname
                    if art_ldap.exist_in_ARTUS_AD(userid):
                        return True
                    else:
                        forename_name = self.get_meggitt_id(userid)
                        if forename_name:
                            return mgt_ldap.exist_in_MEGGITT_AD(forename_name)
                        else:
                            return False
                        
    def get_meggitt_mail(self, userid):
        with Meggitt_Ldap() as mgt_ldap:
            with Artus_Ldap() as art_ldap:
                if '.' in userid:
                    # forename.name
                    return mgt_ldap.get_meggitt_mail(userid)
                else:
                    # fname
                    if art_ldap.exist_in_ARTUS_AD(userid):
                        return art_ldap.get_artus_mail(userid)
                    else:
                        forename_name = self.get_meggitt_id(userid)
                        if forename_name:
                            return mgt_ldap.get_meggitt_mail(forename_name)
                        else:
                            return None

    def get_ldap_displayname(self, userid):
        with Meggitt_Ldap() as mgt_ldap:
            return mgt_ldap.get_ldap_displayname(userid)
        
    def user_is_external(self, userid):
        meggitt_mail = self.get_meggitt_mail(userid)
        if meggitt_mail:
            return '.external@' in self.get_meggitt_mail(userid)
        else:
            return False

