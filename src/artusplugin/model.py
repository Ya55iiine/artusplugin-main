# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Artus
# All rights reserved.
#
# Author: Michel Guillot <michel.guillot@meggitt.com>

# Trac
from trac.core import TracError
from trac.resource import ResourceNotFound
from trac.util.datefmt import to_utimestamp, utc

# Standard lib
from datetime import datetime
import re

# Same package
from artusplugin import util, _
from artusplugin.buildbot.model import Build

__all__ = ['Tag', 'BaselineItem', 'Document', 'Drl', 'DrlItem',
           'AttachmentCustom', 'Branch']


class NamingRule:
    """ Defines configuration item names templates """

    @staticmethod
    def get_ci_name_pattern(ProgramIdRE, SkillRE, tag, ciType, versionType):
        # tag is True for tags under /tags and False under /trunk or /branches
        # CI name is given in group ci_name
        ci_name_pattern = ("(?P<ci_name>%s_(?:%s)(?:_[^\W_]+"
                           "(?:-?(?<=-)[^\W_]+)*){1,2})" % (
                               ProgramIdRE, SkillRE))
        if tag:
            if ciType == 'document':
                ci_name_pattern += "_\d+\.\d+\.(?:Draft|Proposed|Released)\d*"
            else:
                if versionType == 'SER':
                    ci_name_pattern += "_\d\d\.\d\d\.\d\d"
                else:
                    ci_name_pattern += "_[A-Z]\d\d(?:\.[A-Z])?"
                ci_name_pattern += "[ECRP](?:\d\d)?"

        return ci_name_pattern

    @staticmethod
    def get_ci_version_pattern(ProgramIdRE, SkillRE, ciType, versionType):
        ci_version_pattern = ("(?P<ci_version>%s_(?:%s)(?:_[^\W_]+"
                              "(?:-?(?<=-)[^\W_]+)*){1,2}" % (
                                  ProgramIdRE, SkillRE))
        if ciType == 'document':
            ci_version_pattern += "_\d+\.\d+\.(?:Draft|Proposed|Released)\d*)"
        else:
            if versionType == 'SER':
                ci_version_pattern += "_\d\d\.\d\d\.\d\d"
            else:
                ci_version_pattern += "_[A-Z]\d\d(?:\.[A-Z])?"
            ci_version_pattern += "[ECRP](?:\d\d)?)"

        return ci_version_pattern

    @staticmethod
    def get_ci_pattern(ProgramIdRE, SkillRE):
        ci_pattern = ("(%s_(?:%s)(?:_(?:[^\W_]|-)+)?_(?:[^\W_]|-)+)(?!_)" % (
                      ProgramIdRE, SkillRE))

        return ci_pattern

    @staticmethod
    def get_version_pattern(ProgramIdRE, SkillRE, ciType, versionType):
        version_pattern = ("%s_(?:%s)_(?:[^\W_]+"
                           "(?:-?(?:(?<=-)[^\W_]+|(?<!-)"
                           "(?=_)))*_){1,2}") % (ProgramIdRE, SkillRE)
        if ciType == 'document':
            version_pattern += "\d+\.\d+"
        else:
            if versionType == 'SER':
                version_pattern += "\d\d\.\d\d\.\d\d"
            else:
                version_pattern += "[A-Z]\d\d(?:\.[A-Z])?"

        return version_pattern

    @staticmethod
    def get_document_pattern(ProgramIdRE, SkillRE, versionType, unmanaged_skills=None):
        document_pattern = ("(?P<project>%s)_(?P<skill>%s)_(?:[^\W_]+"
                            "(?:-?(?:(?<=-)[^\W_]+|(?<!-)(?=_)))*_){1,2}" % (
                                ProgramIdRE, SkillRE))
        if versionType in ('SER', 'ER', 'MA'):
            if versionType == 'SER':
                # Standard / Edition / Revision
                document_pattern += "(?P<standard>\d+)\.(?P<edition>\d+)\.(?P<revision>\d+)\."
            elif versionType == 'ER':
                # Edition / Revision
                document_pattern += "(?P<edition>\d+)\.(?P<revision>\d+)\."
            else:
                # Modification / Amendment
                document_pattern += "(?P<modification>[A-Z]\d\d)(?:\.(?P<amendment>[A-Z]))?\."
            document_pattern += "(?P<status>Draft|Proposed|Released)(?P<status_index>\d*)"
        else:
            # Unmanaged
            document_pattern = ("(?P<project>%s)_(?P<skill>(?:%s))_(?P<clientref>[^\s]+)" % (
                                ProgramIdRE, unmanaged_skills))

        return document_pattern

    @staticmethod
    def get_ecm_pattern(ProgramIdRE):
        ecm_pattern = ("ECM_(?P<project>%s)_(?P<chrono>\d{3}(?<!000))"
                       "_(?P<version>v[1-9]{0,1}(?:(?<=[1-9])0|[1-9]))"
                       "(?:\.(?P<version_status>\d(?:(?<=[1-9])0|[1-9])))?" % ProgramIdRE)

        return ecm_pattern

    @staticmethod
    def get_fee_pattern(ProgramIdRE):
        fee_pattern = ("FEE_(?P<project>%s)_(?P<chrono1>\d{5}(?<!00000))-(?P<chrono2>\d{2})"
                       "_(?P<version>v[1-9]{0,1}(?:(?<=[1-9])0|[1-9]))"
                       "(?:\.(?P<version_status>\d(?:(?<=[1-9])0|[1-9])))?" % ProgramIdRE)

        return fee_pattern

    @staticmethod
    def get_component_pattern(ProgramIdRE, SkillRE, versionType):
        component_pattern = ("(?P<project>%s)_(?P<skill>%s)_"
                             "(?:[^\W_]+(?:-?(?:(?<=-)[^\W_]+|(?<!-)(?=_)))*_){1,2}" % (
                                 ProgramIdRE, SkillRE))
        if versionType == 'SER':
            component_pattern += "(?P<standard>\d\d)\.(?P<edition>\d\d)\.(?P<revision>\d\d)"
        else:
            component_pattern += "(?:(?P<standard>\d+)\.)?(?P<modification>[A-Z]\d\d)(?:\.(?P<amendment>[A-Z]))?"
        component_pattern += "(?P<status>[ECRP])(?P<status_index>(?:\d\d)?)"

        return component_pattern

    @staticmethod
    def get_component_groups(versionType):
        if versionType == 'SER':
            component_groups = ['tag_name', 'project', 'skill', 'standard',
                                'edition', 'revision', 'status', 'status_index']
        else:
            component_groups = ['tag_name', 'project', 'skill', 'modification',
                                'amendment', 'status', 'status_index']

        return component_groups

    @staticmethod
    def get_milestone_pattern(ProgramIdRE, SkillRE):
        # cf Développement#100
        milestone_pattern = ("(?P<project>%s)_(?P<skill>%s)_"
                             "(?P<review>[A-Za-z0-9-.]+)\.(?P<status>Prepared|Reviewed|Accepted)"
                             "(?P<status_index>\d*)" % (ProgramIdRE, SkillRE))

        return milestone_pattern

    @staticmethod
    def get_shortname(env, tagname, program_name):
        """ return shortname of a document or component if feasible """
        # default return value
        shortname = None
        tg = Tag(env, name=tagname)
        # If not a milestone and not unmanaged
        if not tg.review and not util.skill_is_unmanaged(env, tagname):
            # Internal documents only have a known formalism
            skill_options = env.config.get('ticket-custom', 'skill.options')
            re_tag = ("%s_(?:%s)_(?:([^\W_]+(?:-?(?:(?<=-)[^\W_]+"
                      "|(?<!-)(?=_)))*)_){1,2}" % (program_name, skill_options))
            if tg.component:
                # component
                if tg.version_type == 0:
                    # S.E.R
                    re_tag += "[0-9][0-9]\.[0-9][0-9]\.[0-9][0-9]"
                else:
                    # M.A.
                    re_tag += "[A-Z][0-9][0-9](?:\.[A-Z])?"
                re_tag += "[ECRP](?:[0-9][0-9])?"
            else:
                # document
                if tg.version_type == 0:
                    # E.R.
                    re_tag += "[1-9]\d*\.(?:0|[1-9]\d*)\."
                else:
                    # M.A.
                    re_tag += "[A-Z][0-9][0-9](?:\.[A-Z])?\."
                re_tag += "(?:Draft[1-9]\d*|Proposed[1-9]\d*|Released)"
            match = re.search(re_tag, tagname, re.UNICODE)
            if match:
                shortname = match.group(1)

        return shortname

    @staticmethod
    def get_branch_from_tag(env, tagname):
        try:
            tg = Tag(env, name=tagname)
            if tg.buildbot == 1 and tg.tag_url:
                db = env.get_db_cnx()
                prod_csci_name = Build(env, (tg.builder, tg.build_no), db=db).CSCI_tag
                csci_tg = Tag(env, name=prod_csci_name)
                return NamingRule.get_branch_from_tag(env, csci_tg.name)
            else:
                if tg.source_url:
                    match = re.search('\A(?:/\w+)?/branches/(B\d+)/.+\Z', tg.source_url)
                    if match:
                        return match.group(1)
                    else:
                        match = re.search('\A(?:/\w+)?/trunk/.+\Z', tg.source_url)
                        if match:
                            return 'trunk'
                        else:
                            return '?'
                else:
                    return '?'
        except ResourceNotFound:
            return '?'

    @staticmethod
    def get_status_from_tag(env, tagname):
        tg = Tag(env, name=tagname)
        return tg.status

    @staticmethod
    def get_version_from_tag(env, tagname):
        tg = Tag(env, name=tagname)
        return tg.tagged_item

    @staticmethod
    def is_ci_name(env, name, prog):
        match = False
        if util.skill_is_unmanaged(env, name):
            # Unmanaged skill
            match = True
        else:
            skill_options = env.config.get('ticket-custom',
                                           'skill.options')
            regular_expression = '^%s$' % NamingRule.get_ci_pattern(
                prog, skill_options)
            if re.search(regular_expression, name, re.UNICODE):
                # Internal CI
                match = True

        return match

    @staticmethod
    def is_tag_name(env, name, prog):
        match = False
        if util.skill_is_unmanaged(env, name):
            # Unmanaged skill
            match = True
        else:
            programidre = env.config.get('artusplugin', 'programidre')
            skillre = env.config.get('ticket-custom', 'skill.options')
            # S.E.R. component
            regular_expression = NamingRule.get_component_pattern(programidre, skillre, 'SER')
            if re.search(regular_expression, name, re.UNICODE):
                match = True
            else:
                # M.A. component
                regular_expression = NamingRule.get_component_pattern(programidre, skillre, 'MA')
                if re.search(regular_expression, name, re.UNICODE):
                    match = True
                else:
                    # E.R. document
                    regular_expression = NamingRule.get_document_pattern(programidre, skillre, 'ER')
                    if re.search(regular_expression, name, re.UNICODE):
                        match = True

        return match

    @staticmethod
    def is_tag_dir(env, name, prog, parent, gd_parent):
        match = False
        # Unmanaged skill ?
        if util.skill_is_unmanaged(env, name):
            # External tag or directory
            if util.skill_is_unmanaged(env, parent) or util.skill_is_unmanaged(env, gd_parent):
                # External tag because parent or grand-parent is an external tag
                match = True
        elif name.startswith('%s_' % prog):
            # Internal tag or trunk directory
            if parent in ['Draft', 'Proposed', 'Released', 'Candidate',
                          'Engineering', 'Patch', 'Prepared', 'Reviewed',
                          'Accepted']:
                # Internal tag because parent is a status
                skill_options = env.config.get('ticket-custom', 'skill.options')
                m = re.search(NamingRule.get_ci_version_pattern(
                    prog,
                    skill_options,
                    'document',
                    'ER'), name, re.UNICODE)
                if m:
                    match = True
                else:
                    m = re.search(NamingRule.get_ci_version_pattern(
                        prog,
                        skill_options,
                        'component',
                        'SER'), name, re.UNICODE)
                    if m:
                        match = True
                    else:
                        m = re.search(NamingRule.get_ci_version_pattern(
                            prog,
                            skill_options,
                            'component',
                            'MA'), name, re.UNICODE)
                        if m:
                            match = True
        elif name.startswith('ECM_%s_' % prog):
            m = re.search(NamingRule.get_ecm_pattern(prog), name, re.UNICODE)
            if m:
                match = True
        elif name.startswith('FEE_%s_' % prog):
            m = re.search(NamingRule.get_fee_pattern(prog), name, re.UNICODE)
            if m:
                match = True

        return match

    @staticmethod
    def split_version_tag(env, tagname, program_name):
        """ return reference, version and status
            of a document or component if feasible """
        # default return values
        reference = tagname
        version = ''
        status = ''
        try:
            tg = Tag(env, name=tagname)
            if not tg.review and not util.skill_is_unmanaged(env, tagname):
                # Managed skill
                programidre = env.config.get('artusplugin', 'programidre')
                skill_options = env.config.get('ticket-custom', 'skill.options')
                if tg.component:
                    # component
                    if tg.version_type == 0:
                        # S.E.R
                        regular_expression = NamingRule.get_component_pattern(
                            programidre, skill_options, 'SER')
                        match = re.search(regular_expression, tagname, re.UNICODE)
                        if match:
                            reference = tagname.rsplit('_', 1)[0]
                            version = "%s.%s.%s" % (match.group('standard'),
                                                    match.group('edition'),
                                                    match.group('revision'))
                            status = "%s%s" % (match.group('status'),
                                               match.group('status_index'))
                    else:
                        # M.A.
                        regular_expression = NamingRule.get_component_pattern(
                            programidre, skill_options, 'MA')
                        match = re.search(regular_expression, tagname, re.UNICODE)
                        if match:
                            reference = tagname.rsplit('_', 1)[0]
                            version = "%s" % match.group('modification')
                            if match.group('amendment'):
                                version = "%s.%s" % (version,
                                                     match.group('amendment'))
                            status = "%s%s" % (match.group('status'),
                                               match.group('status_index'))
                else:
                    # document
                    if tg.version_type == 0:
                        # E.R.
                        regular_expression = NamingRule.get_document_pattern(
                            programidre, skill_options, 'ER')
                        match = re.search(regular_expression, tagname, re.UNICODE)
                        if match:
                            reference = tagname.rsplit('_', 1)[0]
                            version = "%s.%s" % (match.group('edition'),
                                                 match.group('revision'))
                            status = "%s%s" % (match.group('status'),
                                               match.group('status_index'))
                    else:
                        # M.A.
                        regular_expression = NamingRule.get_document_pattern(
                            programidre, skill_options, 'MA')
                        match = re.search(regular_expression, tagname, re.UNICODE)
                        if match:
                            reference = tagname.rsplit('_', 1)[0]
                            version = "%s" % match.group('modification')
                            if match.group('amendment'):
                                version = "%s.%s" % (version,
                                                     match.group('amendment'))
                            status = "%s%s" % (match.group('status'),
                                               match.group('status_index'))
        except ResourceNotFound:
            # No check for unmanaged skills:
            # only the document reference is copied by TRAC
            if not util.skill_is_unmanaged(env, tagname):
                # Internal document or component
                # As the tag has been manually created,
                # there may be formalism errors
                # This is for legacy projects, minimal checks are done
                try:
                    if 'Draft' in tagname or 'Proposed' in tagname or 'Released' in tagname:
                        # Document
                        reference, splitted_part = tagname.rsplit('_', 1)
                        version, status = splitted_part.rsplit('.', 1)
                    else:
                        # Component
                        reference, splitted_part = tagname.rsplit('_', 1)
                        version, status, status_index = re.split(r"([ECRP])", str, flags=re.I)
                        status += status_index
                except Exception:
                    raise TracError("Errors were found on tag name %s" % tagname)
            else:
                # External document
                reference = tagname
                version = ''
                status = ''

        return reference, version, status


def simplify_whitespace(name):
    """Strip spaces and remove duplicate spaces within names"""
    if name:
        return ' '.join(name.split())
    else:
        return name


class Tag(object):

    def __init__(self, env, name=None, db=None):
        self.env = env
        self.program_name = self.env.config.get('project', 'descr')
        if (self.program_name != 'SB' and self.program_name.endswith('SB')) or self.program_name.endswith('FF'):
            self.program_name = self.program_name[:-2]
        if not db:
            db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM sqlite_master "
                       "WHERE tbl_name='tag'")
        row = cursor.fetchone()
        if not row:
            raise ResourceNotFound(_('Table "tag" does not exist.'))
        if name:
            name = simplify_whitespace(name)
        if name:
            cursor = db.cursor()
            cursor.execute("SELECT * FROM tag "
                           "WHERE name=%s", (name,))
            row = cursor.fetchone()
            if not row:
                raise ResourceNotFound(_('Tag %(name)s does not exist.',
                                         name=name))
            self.name = self._old_name = name
            self.tagged_item = row[1]
            self.tracked_item = row[2]
            self.author = row[3]
            self.review = row[4]
            self.standard = row[5]
            self.edition = row[6]
            self.revision = row[7]
            self.modification = row[8]
            self.amendment = row[9]
            self.status = row[10]
            self.status_index = row[11]
            self.source_url = row[12]
            self.tag_url = row[13]
            self.component = row[14]
            self.baselined = row[15]
            self.buildbot = row[16]
            self.builder = row[17]
            self.build_no = row[18]
            self.version_type = row[19]
            self.tag_refs = row[20]
        else:
            self.name = self._old_name = None
            self.tagged_item = None
            self.tracked_item = None
            self.author = None
            self.review = None
            self.standard = None
            self.edition = None
            self.revision = None
            self.modification = None
            self.amendment = None
            self.status = None
            self.status_index = None
            self.source_url = None
            self.tag_url = None
            self.component = None
            self.baselined = None
            self.buildbot = None
            self.builder = None
            self.build_no = None
            self.version_type = None
            self.tag_refs = None

    exists = property(fget=lambda self: self._old_name is not None)

    def _check_integrity(self):
        check_integrity = self.env.config.get('artusplugin', 'check_integrity', 'true')
        if check_integrity not in ('true', 'True'):
            return
        programidre = self.env.config.get('artusplugin', 'programidre')
        skill_options = self.env.config.get('ticket-custom', 'skill.options')
        branch_segregation_activated = True if self.env.config.get('artusplugin', 'branch_segregation_activated') == 'True' else False

        if self.review is not None:  # milestone
            milestone_pattern = NamingRule().get_milestone_pattern(programidre,
                                                                   skill_options)
            match = re.search(milestone_pattern, self.name, re.UNICODE)
            if not match:
                raise TracError(_('milestone tag name (%s) does not match re "%s"'
                                  % (self.name, milestone_pattern)))
            if not self.name.startswith(self.tagged_item):
                raise TracError(_('integrity error on "tagged_item"'))
            m = match.group('skill')
            if not self.tracked_item == m:
                raise TracError(_('integrity error on "tracked_item"'))
            m = match.group('review')
            if not self.review == m:
                raise TracError(_('integrity error on "review"'))
            m = match.group('status')
            if not self.status == m:
                raise TracError(_('integrity error on "status"'))
            m = match.group('status_index')
            if m:
                if not int(self.status_index) == int(m):
                    raise TracError(_('integrity error on "status_index"'))

        elif self.modification is not None:  # component (M.A.)
            component_pattern = NamingRule().get_component_pattern(programidre,
                                                                   skill_options,
                                                                   'MA')
            match = re.search(component_pattern, self.name, re.UNICODE)
            if not match:
                raise TracError(_('version tag name (%s) does not match re "%s"'
                                  % (self.name, component_pattern)))
            if not self.name.startswith(self.tagged_item):
                raise TracError(_('integrity error on "tagged_item"'))
            if not self.name.startswith(self.tracked_item):
                raise TracError(_('integrity error on "tracked_item"'))
            m = match.group('standard')
            if (self.standard is None and m is not None or
                self.standard is not None and m is None):
                raise TracError(_('integrity error on "standard"'))
            if (self.standard is not None and
                m is not None and
                not int(self.standard) == int(m)):
                raise TracError(_('integrity error on "standard"'))
            m = match.group('modification')
            if not self.modification == m:
                raise TracError(_('integrity error on "modification"'))
            m = match.group('amendment')
            if m:
                if not self.amendment == m:
                    raise TracError(_('integrity error on "amendment"'))
            m = match.group('status')
            if not self.status[0] == m:
                raise TracError(_('integrity error on "status"'))
            m = match.group('status_index')
            if m:
                if not int(self.status_index) == int(m):
                    raise TracError(_('integrity error on "status_index"'))

        elif self.component:  # component (S.E.R.)
            component_pattern = NamingRule().get_component_pattern(programidre,
                                                                   skill_options,
                                                                   'SER')
            match = re.search(component_pattern, self.name, re.UNICODE)
            if not match:
                raise TracError(_('version tag name (%s) does not match re "%s"'
                                  % (self.name, component_pattern)))
            if not self.name.startswith(self.tagged_item):
                raise TracError(_('integrity error on "tagged_item"'))
            if not self.name.startswith(self.tracked_item):
                raise TracError(_('integrity error on "tracked_item"'))
            m = match.group('standard')
            if not int(self.standard) == int(m):
                raise TracError(_('integrity error on "standard"'))
            m = match.group('edition')
            if not int(self.edition) == int(m):
                raise TracError(_('integrity error on "edition"'))
            m = match.group('revision')
            if not int(self.revision) == int(m):
                raise TracError(_('integrity error on "revision"'))
            m = match.group('status')
            if not self.status[0] == m:
                raise TracError(_('integrity error on "status"'))
            m = match.group('status_index')
            if m:
                if not int(self.status_index) == int(m):
                    raise TracError(_('integrity error on "status_index"'))

        else:
            if util.skill_is_unmanaged(self.env, self.name):
                # No integrity check for unmanaged skills
                pass
            elif self.name.startswith('ECM_'):
                # ECM record
                ecm_pattern = NamingRule().get_ecm_pattern(programidre)
                match = re.search(ecm_pattern, self.name)
                if not match:
                    raise TracError(_('version tag name (%s) does not match re "%s"'
                                      % (self.name, ecm_pattern)))
                if not self.name.startswith(self.tagged_item):
                    raise TracError(_('integrity error on "tagged_item"'))
                if not self.name.startswith(self.tracked_item):
                    raise TracError(_('integrity error on "tracked_item"'))
                m = match.group('version_status')
                if m:
                    if not int(self.status_index) == int(m):
                        raise TracError(_('integrity error on "status_index"'))
            elif self.name.startswith('FEE_'):
                # FEE record
                fee_pattern = NamingRule().get_fee_pattern(programidre)
                match = re.search(fee_pattern, self.name)
                if not match:
                    raise TracError(_('version tag name (%s) does not match re "%s"'
                                      % (self.name, fee_pattern)))
                if not self.name.startswith(self.tagged_item):
                    raise TracError(_('integrity error on "tagged_item"'))
                if not self.name.startswith(self.tracked_item):
                    raise TracError(_('integrity error on "tracked_item"'))
                m = match.group('version_status')
                if m:
                    if not int(self.status_index) == int(m):
                        raise TracError(_('integrity error on "status_index"'))
            else:
                # internal document
                version_type = 'SER' if branch_segregation_activated and self.standard not in (u'0', 0, None) else 'ER'
                document_pattern = NamingRule().get_document_pattern(programidre,
                                                                     skill_options,
                                                                     version_type)
                match = re.search(document_pattern, self.name, re.UNICODE)
                if not match:
                    raise TracError(_('version tag name (%s) does not match re "%s"'
                                      % (self.name, document_pattern)))
                if not self.name.startswith(self.tagged_item):
                    raise TracError(_('integrity error on "tagged_item"'))
                if not self.name.startswith(self.tracked_item):
                    raise TracError(_('integrity error on "tracked_item"'))
                if version_type == 'SER':
                    m = match.group('standard')
                    if not int(self.standard) == int(m):
                        raise TracError(_('integrity error on "standard"'))
                m = match.group('edition')
                if not int(self.edition) == int(m):
                    raise TracError(_('integrity error on "edition"'))
                m = match.group('revision')
                if not int(self.revision) == int(m):
                    raise TracError(_('integrity error on "revision"'))
                m = match.group('status')
                if not self.status == m:
                    raise TracError(_('integrity error on "status"'))
                m = match.group('status_index')
                if m:
                    if not int(self.status_index) == int(m):
                        raise TracError(_('integrity error on "status_index"'))

    def delete(self, db=None):
        assert self.exists, 'Cannot delete non-existent Tag'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info('Deleting Tag %s' % self.name)
        cursor.execute("DELETE FROM tag WHERE name=%s", (self.name,))

        self.name = self._old_name = None

        if handle_ta:
            db.commit()

    def insert(self, db=None):
        assert not self.exists, 'Cannot insert existing Tag'
        self.name = simplify_whitespace(self.name)
        assert self.name, 'Cannot create Tag with no name'
        self._check_integrity()
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.debug("Creating new Tag '%s'" % self.name)
        sql = ("INSERT INTO tag (name,tagged_item,tracked_item,author,"
               "review,standard,edition,revision,modification,amendment,"
               "status,status_index,source_url,tag_url,component,baselined,"
               "buildbot,builder,build_no,version_type,tag_refs) "
               "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
        cursor.execute(sql,
                       (self.name,
                        self.tagged_item,
                        self.tracked_item,
                        self.author,
                        self.review,
                        self.standard,
                        self.edition,
                        self.revision,
                        self.modification,
                        self.amendment,
                        self.status,
                        self.status_index,
                        self.source_url,
                        self.tag_url,
                        self.component,
                        self.baselined,
                        self.buildbot,
                        self.builder,
                        self.build_no,
                        self.version_type,
                        self.tag_refs))

        if handle_ta:
            db.commit()

    def update(self, db=None):
        assert self.exists, 'Cannot update non-existent Tag'
        self.name = simplify_whitespace(self.name)
        assert self.name, 'Cannot update Tag with no name'
        self._check_integrity()
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info('Updating Tag "%s"' % self.name)
        sql = ("UPDATE tag SET name=%s,tagged_item=%s,tracked_item=%s,"
               "author=%s,review=%s,standard=%s,edition=%s,revision=%s,"
               "modification=%s,amendment=%s,status=%s,status_index=%s,"
               "source_url=%s,tag_url=%s,component=%s,baselined=%s,"
               "buildbot=%s,builder=%s,build_no=%s,version_type=%s,"
               "tag_refs=%s WHERE name=%s")
        cursor.execute(sql,
                       (self.name,
                        self.tagged_item,
                        self.tracked_item,
                        self.author,
                        self.review,
                        self.standard,
                        self.edition,
                        self.revision,
                        self.modification,
                        self.amendment,
                        self.status,
                        self.status_index,
                        self.source_url,
                        self.tag_url,
                        self.component,
                        self.baselined,
                        self.buildbot,
                        self.builder,
                        self.build_no,
                        self.version_type,
                        self.tag_refs,
                        self._old_name))

        if handle_ta:
            db.commit()

    def select(self, env, where_expr_list=[], ordering_term='name ASC', db=None, tag_type='version_tags'):
        if not db:
            db = env.get_db_cnx()
        cursor = db.cursor()

        terms = [term.strip() for term in ordering_term.split(',')]
        terms = [tuple(term.split() if ' ' in term else (term, 'ASC')) for term in terms]

        if tag_type == 'version_tags':
            sql = "SELECT * FROM tag WHERE review is NULL "
            for expr in where_expr_list:
                if expr:
                    sql += "AND " + expr + " "
            ordering_terms = []
            for (header, sort_order) in terms:
                if header == 'name':
                    ordering_terms.append("tracked_item %s, standard %s, edition %s, revision %s, modification %s, amendment %s, status %s, status_index %s" % (8*(sort_order,)))
                elif header == 'rev':
                    ordering_terms.append("abs(substr(tag_url, length(replace(tag_url, '?rev=', X'00'))+6)) %s" % sort_order)
                else:
                    ordering_terms.append("%s %s" % (header, sort_order))
            sql += "ORDER BY %s" %  ','.join(ordering_terms)
        else:
            sql = "SELECT * FROM tag WHERE review is not NULL "
            for expr in where_expr_list:
                if expr:
                    sql += "AND " + expr + " "
            ordering_terms = []
            for (header, sort_order) in terms:
                if header == 'name':
                    ordering_terms.append("tracked_item %s, review %s, status %s, status_index %s" % (4*(sort_order,)))
                elif header == 'rev':
                    ordering_terms.append("abs(substr(tag_url, length(replace(tag_url, '?rev=', X'00'))+6)) %s" % sort_order)
                else:
                    ordering_terms.append("%s %s" % (header, sort_order))
            sql += "ORDER BY %s" %  ','.join(ordering_terms)

        cursor.execute(sql)
        for (name,
             tagged_item,
             tracked_item,
             author,
             review,
             standard,
             edition,
             revision,
             modification,
             amendment,
             status,
             status_index,
             source_url,
             tag_url,
             component,
             baselined,
             buildbot,
             builder,
             build_no,
             version_type,
             tag_refs) in cursor:
            record = self(env)
            record.name = record._old_name = name
            record.tagged_item = tagged_item
            record.tracked_item = tracked_item
            record.author = author
            record.review = review
            record.standard = standard
            record.edition = edition
            record.revision = revision
            record.modification = modification
            record.amendment = amendment
            record.status = status
            record.status_index = status_index
            record.source_url = source_url
            record.tag_url = tag_url
            record.component = component
            record.baselined = baselined
            record.buildbot = buildbot
            record.builder = builder
            record.build_no = build_no
            record.version_type = version_type
            record.tag_refs = tag_refs
            yield record

    select = classmethod(select)


class BaselineItem(object):

    def __init__(self, env, primary_key=None, db=None):
        self.env = env
        if not db:
            db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM sqlite_master "
                       "WHERE tbl_name='baseline_item'")
        row = cursor.fetchone()
        if not row:
            raise ResourceNotFound(_('Table "baseline_item" does not exist.'))
        if primary_key:
            name, baselined_tag = primary_key
            name = simplify_whitespace(name)
            baselined_tag = simplify_whitespace(baselined_tag)
            cursor = db.cursor()
            cursor.execute("SELECT * FROM baseline_item "
                           "WHERE name=%s AND baselined_tag=%s", (name, baselined_tag))
            row = cursor.fetchone()
            if not row:
                raise ResourceNotFound(_('Tag %(name)s is not included in Baseline %(baselined_tag)s.',
                                         name=name, baselined_tag=baselined_tag))
            self.name = self._old_name = name
            self.baselined_tag = self._old_baselined_tag = baselined_tag
            self.author = row[2]
            self.subpath = row[3]
        else:
            self.name = self._old_name = None
            self.baselined_tag = self._old_baselined_tag = None
            self.author = None
            self.subpath = None

    exists = property(fget=lambda self: self._old_name is not None and self._old_baselined_tag is not None)

    def delete(self, db=None):
        assert self.exists, 'Cannot delete non-existent Baseline Item'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info('Deleting Baseline Item %s from Baseline %s' % (self.name, self.baselined_tag))
        cursor.execute("DELETE FROM baseline_item WHERE name=%s AND baselined_tag=%s", (self.name, self.baselined_tag))

        self.name = self._old_name = None
        self.baselined_tag = self._old_baselined_tag = None

        if handle_ta:
            db.commit()

    def insert(self, db=None):
        assert not self.exists, 'Cannot insert existing Baseline Item'
        self.name = simplify_whitespace(self.name)
        self.baselined_tag = simplify_whitespace(self.baselined_tag)
        assert self.name, 'Cannot create Baseline Item with no name'
        assert self.baselined_tag, 'Cannot create Baseline Item with no baselined tag'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.debug("Creating new Baseline Item '%s'" % self.name)
        cursor.execute("INSERT INTO baseline_item (name,baselined_tag,author,subpath) "
                       "VALUES (%s,%s,%s,%s)",
                       (self.name, self.baselined_tag, self.author, self.subpath))

        if handle_ta:
            db.commit()

    def update(self, db=None):
        assert self.exists, 'Cannot update non-existent Baseline Item'
        self.name = simplify_whitespace(self.name)
        self.baselined_tag = simplify_whitespace(self.baselined_tag)
        assert self.name, 'Cannot update Baseline Item with no name'
        assert self.baselined_tag, 'Cannot create Baseline Item with no baselined tag'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info('Updating Baseline Item "%s" for baseline "%s"' % (self.name, self.baselined_tag))
        cursor.execute("UPDATE baseline_item SET name=%s,baselined_tag=%s,author=%s,subpath=%s "
                       "WHERE name=%s and baselined_tag=%s",
                       (self.name, self.baselined_tag, self.author, self.subpath,
                        self._old_name, self._old_baselined_tag))

        if handle_ta:
            db.commit()

    def select(self, env, where_expr_list=None, ordering_term='name ASC', db=None):
        if not db:
            db = env.get_db_cnx()
        cursor = db.cursor()
        sql = "SELECT * FROM baseline_item "
        if where_expr_list:
            sql += "WHERE " + where_expr_list[0] + " "
            del where_expr_list[0]
            for expr in where_expr_list:
                sql += "AND " + expr + " "
        sql += "ORDER BY " + ordering_term
        cursor.execute(sql)
        for name, baselined_tag, author, subpath in cursor:
            record = self(env)
            record.name = record._old_name = name
            record.baselined_tag = record._old_baselined_tag = baselined_tag
            record.author = author
            record.subpath = subpath
            yield record

    select = classmethod(select)


class Document(object):

    def __init__(self, env, name=None, db=None):
        self.env = env
        if not db:
            db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM sqlite_master "
                       "WHERE tbl_name='document'")
        row = cursor.fetchone()
        if not row:
            raise ResourceNotFound(_('Table "document" does not exist.'))
        if name:
            name = simplify_whitespace(name)
        self.values = {}
        self._old = {}
        if name:
            cursor = db.cursor()
            cursor.execute("SELECT * FROM document "
                           "WHERE name=%s", (name,))
            row = cursor.fetchone()
            if not row:
                raise ResourceNotFound(_('Document %(name)s does not exist.',
                                         name=name))
            self.values['name'] = name
            self.values['shortname'] = row[1]
            self.values['description'] = row[2]
            self.values['builder'] = row[3]
            self.values['source'] = row[4]
            self.values['controlcategory'] = row[5]
            self.values['independence'] = row[6]
            self.values["sourcetype"] = row[7]
            self.values["pdfsigned"] = row[8]
            self.values["submittedfor"] = row[9]
        else:
            self.values['name'] = None
            self.values['shortname'] = None
            self.values['description'] = None
            self.values['builder'] = None
            self.values['source'] = None
            self.values['controlcategory'] = None
            self.values['independence'] = None
            self.values["sourcetype"] = None
            self.values["pdfsigned"] = None
            self.values["submittedfor"] = None

    exists = property(fget=lambda self: self.values['name'] is not None)

    def __getitem__(self, name):
        return self.values.get(name)

    def __setitem__(self, name, value):
        """Log document modifications so the table document_change can be updated
        """
        if name in self.values and self.values[name] == value:
            return
        if name not in self._old:  # Changed field
            self._old[name] = self.values.get(name)
        elif self._old[name] == value:  # Change of field reverted
            del self._old[name]
        if value:
            if isinstance(value, list):
                raise TracError(_("Multi-values fields not supported yet"))
        self.values[name] = value

    def save_changes(self, author, when=None):
        self.update()
        if when is None:
            when = datetime.now(utc)
        when_ts = to_utimestamp(when)
        for name in self._old.keys():
            with self.env.db_transaction as db:
                db("""INSERT INTO document_change
                        (document,time,author,field,oldvalue,newvalue)
                      VALUES (%s, %s, %s, %s, %s, %s)
                      """, (self.values['name'], when_ts, author, name,
                            self._old[name], self.values[name]))
        self._old = {}

    def delete(self, db=None):
        assert self.exists, 'Cannot delete a Document not specified'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info('Deleting Document %s' % self.values['name'])
        cursor.execute("DELETE FROM document WHERE name=%s", (self.values['name'],))

        self.values['name'] = None
        self._old = {}

        if handle_ta:
            db.commit()

    def insert(self, db=None):
        self.values['name'] = simplify_whitespace(self.values['name'])
        assert self.values['name'], 'Cannot create Document with no name'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.debug("Creating new Document '%s'" % self.values['name'])
        cursor.execute("INSERT INTO document "
                       "(name,shortname,description,"
                       "builder,source,"
                       "controlcategory,independence,"
                       "sourcetype, pdfsigned, submittedfor) "
                       "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                       (self.values['name'], self.values['shortname'],
                        self.values['description'], self.values['builder'],
                        self.values['source'], self.values['controlcategory'],
                        self.values['independence'], self.values["sourcetype"],
                        self.values["pdfsigned"], self.values["submittedfor"]))

        if handle_ta:
            db.commit()

    def update(self, db=None):
        assert self.exists, 'Cannot update a Document not specified'
        self.values['name'] = simplify_whitespace(self.values['name'])
        assert self.values['name'], 'Cannot update Document with no name'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info('Updating Document "%s"' % self.values['name'])
        cursor.execute("UPDATE document SET name=%s,shortname=%s,description=%s,"
                       "builder=%s,source=%s,"
                       "controlcategory=%s,independence=%s,"
                       "sourcetype=%s,pdfsigned=%s,submittedfor=%s "
                       "WHERE name=%s",
                       (self.values['name'], self.values['shortname'],
                        self.values['description'], self.values['builder'],
                        self.values['source'], self.values['controlcategory'],
                        self.values['independence'], self.values["sourcetype"],
                        self.values["pdfsigned"], self.values['submittedfor'],
                        self.values['name']))

        if handle_ta:
            db.commit()

    def select(self, env, where_expr_list=None, ordering_term='name ASC', db=None):
        if not db:
            db = env.get_db_cnx()
        cursor = db.cursor()
        sql = "SELECT * FROM document "
        if where_expr_list:
            sql += "WHERE " + where_expr_list[0] + " "
            del where_expr_list[0]
            for expr in where_expr_list:
                sql += "AND " + expr + " "
        sql += "ORDER BY " + ordering_term
        cursor.execute(sql)
        for (name, shortname, description,
             builder, source,
             controlcategory, independence,
             sourcetype, pdfsigned, submittedfor) in cursor:
            record = self(env)
            record.values['name'] = name
            record.values['shortname'] = shortname
            record.values['description'] = description
            record.values['builder'] = builder
            record.values['source'] = source
            record.values['controlcategory'] = controlcategory
            record.values['independence'] = independence
            record.values["sourcetype"] = sourcetype
            record.values["pdfsigned"] = pdfsigned
            record.values["submittedfor"] = submittedfor
            yield record

    select = classmethod(select)


class Drl(object):

    def __init__(self, env, name=None, db=None):
        self.env = env
        if not db:
            db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM sqlite_master "
                       "WHERE tbl_name='drl'")
        row = cursor.fetchone()
        if not row:
            raise ResourceNotFound(_('Table "drl" does not exist.'))
        if name:
            name = simplify_whitespace(name)
        if name:
            cursor = db.cursor()
            cursor.execute("SELECT * FROM drl "
                           "WHERE name=%s", (name,))
            row = cursor.fetchone()
            if not row:
                raise ResourceNotFound(_('Drl %(name)s does not exist.',
                                         name=name))
            self.name = self._old_name = name
            self.description = row[1]
        else:
            self.name = self._old_name = None
            self.description = None

    exists = property(fget=lambda self: self._old_name is not None)

    def delete(self, db=None):
        assert self.exists, 'Cannot delete non-existent Drl'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info('Deleting Drl %s' % self.name)
        cursor.execute("DELETE FROM drl WHERE name=%s", (self.name,))

        self.name = self._old_name = None

        if handle_ta:
            db.commit()

    def insert(self, db=None):
        assert not self.exists, 'Cannot insert existing Drl'
        self.name = simplify_whitespace(self.name)
        assert self.name, 'Cannot create Drl with no name'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.debug("Creating new Drl '%s'" % self.name)
        cursor.execute("INSERT INTO drl (name,description) "
                       "VALUES (%s,%s)",
                       (self.name, self.description))

        if handle_ta:
            db.commit()

    def update(self, db=None):
        assert self.exists, 'Cannot update non-existent Drl'
        self.name = simplify_whitespace(self.name)
        assert self.name, 'Cannot update Drl with no name'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info('Updating Drl "%s"' % self.name)
        cursor.execute("UPDATE drl SET name=%s,description=%s "
                       "WHERE name=%s",
                       (self.name, self.description,
                        self._old_name))

        if handle_ta:
            db.commit()

    def select(self, env, where_expr_list=None, ordering_term='name ASC', db=None):
        if not db:
            db = env.get_db_cnx()
        cursor = db.cursor()
        sql = "SELECT * FROM drl "
        if where_expr_list:
            sql += "WHERE " + where_expr_list[0] + " "
            del where_expr_list[0]
            for expr in where_expr_list:
                sql += "AND " + expr + " "
        sql += "ORDER BY " + ordering_term
        cursor.execute(sql)
        for name, description in cursor:
            record = self(env)
            record.name = record._old_name = name
            record.description = description
            yield record

    select = classmethod(select)


class DrlItem(object):

    def __init__(self, env, primary_key=None, db=None):
        self.env = env
        if not db:
            db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM sqlite_master "
                       "WHERE tbl_name='drl_item'")
        row = cursor.fetchone()
        if not row:
            raise ResourceNotFound(_('Table "drl_item" does not exist.'))
        if primary_key:
            name, drl = primary_key
            name = simplify_whitespace(name)
            drl = simplify_whitespace(drl)
            cursor = db.cursor()
            cursor.execute("SELECT * FROM drl_item "
                           "WHERE name=%s AND drl=%s", (name, drl))
            row = cursor.fetchone()
            if not row:
                raise ResourceNotFound(_('Document %(name)s is not included in Drl %(drl)s.',
                                         name=name, drl=drl))
            self.name = self._old_name = name
            self.drl = self._old_drl = drl
        else:
            self.name = self._old_name = None
            self.drl = None

    exists = property(fget=lambda self: self._old_name is not None and self._old_drl is not None)

    def delete(self, db=None):
        assert self.exists, 'Cannot delete non-existent Drl Item'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info('Deleting Document %s from Drl %s' % (self.name, self.drl))
        cursor.execute("DELETE FROM drl_item WHERE name=%s and drl=%s", (self.name, self.drl))

        self.name = self._old_name = None
        self.drl = self._old_drl = None

        if handle_ta:
            db.commit()

    def insert(self, db=None):
        assert not self.exists, 'Cannot insert existing Drl Item'
        self.name = simplify_whitespace(self.name)
        self.drl = simplify_whitespace(self.drl)
        assert self.name, 'Cannot create Drl Item with no name'
        assert self.drl, 'Cannot create Drl Item with no drl'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.debug("Creating new Drl Item '%s' for Drl %s" % (self.name, self.drl))
        cursor.execute("INSERT INTO drl_item (name,drl) "
                       "VALUES (%s,%s)",
                       (self.name, self.drl))

        if handle_ta:
            db.commit()

    def update(self, db=None):
        assert self.exists, 'Cannot update non-existent Drl Item'
        self.name = simplify_whitespace(self.name)
        self.drl = simplify_whitespace(self.drl)
        assert self.name, 'Cannot update Drl Item with no name'
        assert self.drl, 'Cannot create Drl Item with no drl'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info('Updating Drl Item "%s" for drl "%s"' % (self.name, self.drl))
        cursor.execute("UPDATE drl_item SET name=%s,drl=%s "
                       "WHERE name=%s and drl=%s",
                       (self.name, self.drl,
                        self._old_name))

        if handle_ta:
            db.commit()

    def select(self, env, where_expr_list=None, ordering_term='name ASC', db=None):
        if not db:
            db = env.get_db_cnx()
        cursor = db.cursor()
        sql = "SELECT * FROM drl_item "
        if where_expr_list:
            sql += "WHERE " + where_expr_list[0] + " "
            del where_expr_list[0]
            for expr in where_expr_list:
                sql += "AND " + expr + " "
        sql += "ORDER BY " + ordering_term
        cursor.execute(sql)
        for name, drl in cursor:
            record = self(env)
            record.name = record._old_name = name
            record.drl = drl
            yield record

    select = classmethod(select)


class AttachmentCustom(object):

    def __init__(self, env, primary_key=None, db=None):
        self.env = env
        if not db:
            db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM sqlite_master "
                       "WHERE tbl_name='attachment_custom'")
        row = cursor.fetchone()
        if not row:
            raise ResourceNotFound(_('Table "attachment_custom" does not exist.'))
        if primary_key:
            ttype, tid, tfilename, tname = primary_key
            ttype = simplify_whitespace(ttype)
            tid = simplify_whitespace(tid)
            tfilename = simplify_whitespace(tfilename)
            tname = simplify_whitespace(tname)
            cursor = db.cursor()
            cursor.execute("SELECT * FROM attachment_custom "
                           "WHERE type=%s AND id=%s AND filename=%s AND name=%s", (ttype, tid, tfilename, tname))
            row = cursor.fetchone()
            if not row:
                raise ResourceNotFound(_('Attachment %(filename)s of %(type)s %(id)s has no custom property %(name)s.',
                                         filename=tfilename, type=ttype, id=tid, name=tname))
            self.type = self._old_type = ttype
            self.id = self._old_id = tid
            self.filename = self._old_filename = tfilename
            self.name = self._old_name = tname
            self.value = row[4]
        else:
            self.type = self._old_type = None
            self.id = self._old_id = None
            self.filename = self._old_filename = None
            self.name = self._old_name = None
            self.value = None

    exists = property(fget=lambda self: self._old_type is not None and self._old_id is not None and self._old_filename is not None and self._old_name is not None)

    def delete(self, db=None):
        assert self.exists, 'Cannot delete non-existent attachment custom property'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info('Deleting custom property %s for attachment %s of %s %s' % (self.name, self.filename, self.type, self.id))
        cursor.execute("DELETE FROM attachment_custom WHERE type=%s AND id=%s AND filename=%s AND name=%s", (self.type, self.id, self.filename, self.name))

        self.type = self._old_type = None
        self.id = self._old_id = None
        self.filename = self._old_filename = None
        self.name = self._old_name = None

        if handle_ta:
            db.commit()

    def insert(self, db=None):
        assert not self.exists, 'Cannot insert existing attachment custom property'
        self.type = simplify_whitespace(self.type)
        assert self.type, 'Cannot create attachment custom property with no parent realm'
        self.id = simplify_whitespace(self.id)
        assert self.id, 'Cannot create attachment custom property with no parent id'
        self.filename = simplify_whitespace(self.filename)
        assert self.filename, 'Cannot create attachment custom property with no attachment name'
        self.name = simplify_whitespace(self.name)
        assert self.name, 'Cannot create attachment custom property with no property name'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.debug("Creating new custom property '%s' for attachment '%s' of %s %s" % (self.name, self.filename, self.type, self.id))
        cursor.execute("INSERT INTO attachment_custom (type,id,filename,name,value) "
                       "VALUES (%s,%s,%s,%s,%s)",
                       (self.type, self.id, self.filename, self.name, self.value))

        if handle_ta:
            db.commit()

    def update(self, db=None):
        assert self.exists, 'Cannot update non-existent attachment custom property'
        self.type = simplify_whitespace(self.type)
        assert self.type, 'Cannot update attachment custom property with no parent realm'
        self.id = simplify_whitespace(self.id)
        assert self.id, 'Cannot update attachment custom property with no parent id'
        self.filename = simplify_whitespace(self.filename)
        assert self.filename, 'Cannot update attachment custom property with no attachment name'
        self.name = simplify_whitespace(self.name)
        assert self.name, 'Cannot update attachment custom property with no property name'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info("Updating custom property '%s' for attachment '%s' of %s %s" % (self.name, self.filename, self.type, self.id))
        cursor.execute("UPDATE attachment_custom SET type=%s,id=%s,filename=%s,name=%s,value=%s "
                       "WHERE type=%s and id=%s and filename=%s and name=%s",
                       (self.type, self.id, self.filename, self.name, self.value,
                        self._old_type, self._old_id, self._old_filename, self._old_name))

        if handle_ta:
            db.commit()

    def select(self, env, where_expr_list=None, ordering_term='name ASC', db=None):
        if not db:
            db = env.get_db_cnx()
        cursor = db.cursor()
        sql = "SELECT * FROM attachment_custom "
        if where_expr_list:
            sql += "WHERE " + where_expr_list[0] + " "
            del where_expr_list[0]
            for expr in where_expr_list:
                sql += "AND " + expr + " "
        sql += "ORDER BY " + ordering_term
        cursor.execute(sql)
        for ttype, tid, tfilename, tname, tvalue in cursor:
            record = self(env)
            record.type = record._old_type = ttype
            record.id = record._old_id = tid
            record.filename = record._old_filename = tfilename
            record.name = record._old_name = tname
            record.value = tvalue
            yield record

    select = classmethod(select)


class Branch(object):

    def __init__(self, env, primary_key=None, db=None):
        self.env = env
        if not db:
            db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM sqlite_master "
                       "WHERE tbl_name='branch'")
        row = cursor.fetchone()
        if not row:
            raise ResourceNotFound(_('Table "branch" does not exist.'))
        if primary_key:
            bid = primary_key
            cursor = db.cursor()
            cursor.execute("SELECT * FROM branch "
                           "WHERE id=%s", (bid,))
            row = cursor.fetchone()
            if not row:
                raise ResourceNotFound(_('Branch %(bid)s does not exist.', bid=bid))
            self.id = self._old_id = bid
            self.author = row[1]
            self.source_url = row[2]
            self.source_tag = row[3]
            self.branch_url = row[4]
            self.description = row[5]
        else:
            self.id = self._old_id = None
            self.author = None
            self.source_url = None
            self.source_tag = None
            self.branch_url = None
            self.description = None

    exists = property(fget=lambda self: self._old_id is not None)

    def delete(self, db=None):
        assert self.exists, 'Cannot delete non-existent branch'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info('Deleting branch %s' % self.id)
        cursor.execute("DELETE FROM branch WHERE id=%s", (self.id,))

        self.id = self._old_id = None

        if handle_ta:
            db.commit()

    def insert(self, db=None):
        assert not self.id, 'Cannot guarantee that the requested branch id will be inserted'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        if self.source_url:
            self.env.log.debug("Creating new branch from source url '%s'" % self.source_url)
        elif self.source_tag:
            self.env.log.debug("Creating new branch from source tag '%s'" % self.source_tag)
        cursor.execute("INSERT INTO branch (id,author,source_url,source_tag,branch_url,description) "
                       "VALUES (NULL,%s,%s,%s,%s,%s)",
                       (self.author, self.source_url, self.source_tag, self.branch_url, self.description))

        if handle_ta:
            db.commit()

    def update(self, db=None):
        assert self.exists, 'Cannot update non-existent branch'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info("Updating branch B'%s'" % self.id)
        cursor.execute("UPDATE branch SET id=%s,author=%s,source_url=%s,source_tag=%s,branch_url=%s,description=%s "
                       "WHERE id=%s",
                       (self.id, self.author, self.source_url, self.source_tag, self.branch_url, self.description,
                        self._old_id))

        if handle_ta:
            db.commit()

    def select(self, env, where_expr_list=None, ordering_term='id ASC', db=None):
        if not db:
            db = env.get_db_cnx()
        cursor = db.cursor()
        sql = "SELECT * FROM branch "
        if where_expr_list:
            sql += "WHERE " + where_expr_list[0] + " "
            del where_expr_list[0]
            for expr in where_expr_list:
                sql += "AND " + expr + " "
        sql += "ORDER BY " + ordering_term
        cursor.execute(sql)
        for bid, author, source_url, source_tag, branch_url, description in cursor:
            record = self(env)
            record.id = record._old_id = bid
            record.author = author
            record.source_url = source_url
            record.source_tag = source_tag
            record.branch_url = branch_url
            record.description = description
            yield record

    select = classmethod(select)

    def refresh(self, env, db):
        assert db, 'The refresh method can only be called in the context of an established connection in which an INSERT has been applied'
        cursor = db.cursor()
        self.env.log.info("Looking for last inserted rowid")
        cursor.execute("SELECT last_insert_rowid()")
        bid, = cursor.fetchone()
        self.id = bid

class Reference(object):

    def __init__(self, env, artusref=None, db=None):
        self.env = env
        if not db:
            db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM sqlite_master "
                       "WHERE tbl_name='reference'")
        row = cursor.fetchone()
        if not row:
            raise ResourceNotFound(_('Table "reference" does not exist.'))
        if artusref:
            artusref = simplify_whitespace(artusref)
        if artusref:
            cursor = db.cursor()
            cursor.execute("SELECT * FROM reference "
                           "WHERE artusref=%s", (artusref,))
            row = cursor.fetchone()
            if not row:
                raise ResourceNotFound(_('Reference %(artusref)s does not exist.',
                                         artusref=artusref))
            self.artusref = self._old_artusref = artusref
            self.customerref = row[1]
            self.default = row[2]
        else:
            self.artusref = self._old_artusref = None
            self.customerref = None
            self.default = None

    exists = property(fget=lambda self: self._old_artusref is not None)

    def delete(self, db=None):
        assert self.exists, 'Cannot delete non-existent reference'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info('Deleting reference %s' % self.artusref)
        cursor.execute("DELETE FROM reference WHERE artusref=%s", (self.artusref,))

        self.artusref = self._old_artusref = None

        if handle_ta:
            db.commit()

    def insert(self, db=None):
        assert not self.exists, 'Cannot insert non-existent reference'
        self.artusref = simplify_whitespace(self.artusref)
        assert self.artusref, 'Cannot create reference without artus part'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.debug("Creating new reference '%s'" % self.artusref)
        cursor.execute("INSERT INTO reference (artusref, customerref, [default]) "
                       "VALUES (%s,%s,%s)",
                       (self.artusref, self.customerref, self.default))

        if handle_ta:
            db.commit()

    def update(self, db=None):
        assert self.exists, 'Cannot update non-existent reference'
        self.artusref = simplify_whitespace(self.artusref)
        assert self.artusref, 'Cannot update reference with no artus part'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info('Updating reference "%s"' % self.artusref)
        cursor.execute("UPDATE reference SET artusref=%s, customerref=%s, [default]=%s "
                       "WHERE artusref=%s",
                       (self.artusref, self.customerref, self.default,
                        self._old_artusref))

        if handle_ta:
            db.commit()

    def select(self, env, where_expr_list=None, ordering_term='artusref ASC', db=None):
        if not db:
            db = env.get_db_cnx()
        cursor = db.cursor()
        sql = "SELECT * FROM reference "
        if where_expr_list:
            sql += "WHERE " + where_expr_list[0] + " "
            del where_expr_list[0]
            for expr in where_expr_list:
                sql += "AND " + expr + " "
        sql += "ORDER BY " + ordering_term
        cursor.execute(sql)
        for artusref, customerref, default in cursor:
            record = self(env)
            record.artusref = record._old_artusref = artusref
            record.customerref = customerref
            record.default = default
            yield record

    select = classmethod(select)
