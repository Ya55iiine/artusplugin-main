# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Artus
# All rights reserved.
#
# Author: Michel Guillot <michel.guillot@meggitt.com>

""" Customizations of ticket processing. """

from __builtin__ import basestring, unicode

# Genshi
from genshi.builder import tag
from genshi.core import TEXT
from genshi.filters.transform import Transformer
from genshi.util import plaintext
from genshi.output import TextSerializer

# Trac
from trac.attachment import Attachment, AttachmentModule, InvalidAttachment
from trac.config import OrderedExtensionsOption
from trac.core import Component, implements, TracError
from trac.mimeview import Context
from trac.perm import PermissionSystem
from trac.resource import get_resource_url, ResourceNotFound
from trac.ticket import Ticket
from trac.ticket.api import TicketSystem, ITicketActionController
from trac.ticket.model import Type, Milestone
from trac.ticket.web_ui import TicketModule
from trac.util import pathjoin, get_reporter_id
from trac.util.compat import partial
from trac.util.datefmt import localtz, pretty_timedelta, \
    get_date_format_hint, user_time, format_datetime, utc, to_utimestamp
from trac.util.text import unicode_quote, unicode_unquote
from trac.timeline.web_ui import TimelineModule
from trac.versioncontrol.api import RepositoryManager, NoSuchChangeset
from trac.versioncontrol.web_ui.browser import IPropertyRenderer
from trac.web import IRequestHandler
from trac.web.api import ITemplateStreamFilter
from trac.web.api import HTTPNotFound #@UnresolvedImport
from trac.web.chrome import add_script, add_stylesheet, add_ctxtnav, \
    add_warning, add_notice, INavigationContributor, ITemplateProvider, \
    Chrome, add_script_data
from trac.web.href import Href
from trac.web.main import IRequestFilter
from trac.wiki.formatter import format_to, format_to_html

# Standard lib
import re, pcre
import syslog
import os
import glob
import sys
from backports import configparser
from collections import OrderedDict
from shutil import copy
from datetime import datetime
import json
from time import sleep
from os.path import splitext

# Same package
from artusplugin import util, model, form, _
import artusplugin.cache as cache


class Ticket_UI(object):

    owner_tip = 'TRAC ID of the next person in charge of the ticket'

    type_tip = {'select': 'ECR and DOC types require an authorized profile',
                'EFR': 'Engineering Failure Report',
                'ECR': 'Engineering Change Request',
                'RF': 'Reading Form',
                'PRF': 'Peer Review Form',
                'MOM': 'Minutes of Meeting',
                'RISK': 'Risk',
                'AI': 'Action Item',
                'MEMO': 'Memo',
                'ECM': 'Engineering Coordination Memo',
                'FEE': 'Equipment Evolution Sheet',
                'DOC': 'Document Version'}
    keywords_tip = 'Enter some keywords describing the ticket'
    skill_tip = {'select': '',
                 'PROJ': 'Project',
                 'SYS': 'System',
                 'HW': 'Hardware',
                 'FW': 'Firmware',
                 'SW': 'Software',
                 'INDUS': 'Industrialization',
                 'DIR': 'Revue de Direction',
                 'QMS': 'Revue du Systeme de Management de la Qualite',
                 'CLI': 'Revue Client',
                 'INT': 'Revue Interne',
                 'PROC-1': 'Piloter l&#39;entreprise',
                 'PROC-2': 'Gerer le SMQ',
                 'PROC-3': 'Gerer les SI',
                 'PROC-4': 'Gerer les RH',
                 'PROC-5': 'Gerer les ressources materielles',
                 'PROC-6': 'Gerer l&#39;EHS',
                 'PROC-7': 'Vendre',
                 'PROC-8-1': 'Actionneurs',
                 'PROC-8-2': 'Conversion',
                 'PROC-8-3': 'Logiciels embarques',
                 'PROC-8-4': 'Machines tournantes',
                 'PROC-9': 'Planifier les activites indus developpement',
                 'PROC-10': 'Planifier les activites indus serie',
                 'PROC-11': 'Produire',
                 'PROC-12': 'Acheter provisionner',
                 'PROC-13': 'Servir apres la vente'}

    @staticmethod
    def get_UI(ticket):
        if ticket['type'] == 'ECM':
            if not ticket.exists or 'ecmtype' in ticket.values:
                ticket_type = 'ECM2'
            else:
                ticket_type = 'ECM1'
        else:
            ticket_type = ticket['type']
        UI_class = getattr(sys.modules['artusplugin.web_ui'], '%s_UI' % ticket_type)
        return UI_class

    @staticmethod
    def field_label(ticket, fieldname):
        ticketbox_fields = Ticket_UI.get_UI(ticket)(None, ticket).ticketbox_fields()
        label = next((field[1][1] for field in ticketbox_fields
                      if field[0] == fieldname), None)
        if not label:
            label = next((field['label'] for field in ticket.fields
                          if field['name'] == fieldname), fieldname)
        return label

    def __init__(self, component, ticket):
        self.component = component
        self.ticket = ticket

        program_data = util.get_program_data(ticket.env)
        self.trac_env_name = program_data['trac_env_name']
        self.program_name = program_data['program_name']
        self.ticket_types = [t.name for t in Type.select(ticket.env)]
        self.ticket_type = ticket['type']
        self.urltracbrowse = ticket.env.base_url + '/browser/tags/'

        self.on_ticket_type_change = 'new_ticket_form("%s");' % self.trac_env_name
        self.on_owner_change = 'update_ticket_identifier("%s", "%s");' % (self.ticket_type, self.program_name)
        self.on_skill_change = 'on_skill_change("%s", "%s", "%s");' % (self.ticket_type, self.program_name, self.urltracbrowse)
        self.on_milestonetag_change = 'on_milestonetag_change("%s", "%s");' % (self.ticket_type, self.program_name)
        self.on_duedate_change = 'on_duedate_change("%s", "%s", "%s");' % (self.ticket_type, self.program_name, self.ticket.id)
        self.custom_fields = {}
        self.excluded_fields = {}
        self.single_fields = []
        self.grouped_fields = []
        self.ticket_edit = {}
        for sub_prop in ['office_suite', 'protocol', 'suffix']:
            prop = 'ticket_edit.%s' % sub_prop
            self.ticket_edit[sub_prop] = util.get_prop_values(ticket.env, prop)
        self.attachment_edit = {}
        for sub_prop in ['office_suite', 'protocol']:
            prop = 'attachment_edit.%s' % sub_prop
            self.attachment_edit[sub_prop] = util.get_prop_values(ticket.env, prop)

    def ticketbox_fields(self, perm=None):
        raise NotImplementedError

    def fields_properties(self, perm=None):
        raise NotImplementedError

    def required_fields(self):
        return []

    def ticket_change_fields(self):
        labels = TicketSystem(self.ticket.env).get_ticket_field_labels()
        change_fields = {'attachment': 'Attachment',
                         'reporter': 'Ticket Creator',
                         'owner': 'Owner',
                         'status': 'Status',
                         'resolution': 'Resolution',
                         'company': 'Ticket Initiator',
                         'submitcomment': 'Comment'}
        change_fields.update(dict([(elt[0],
                                    elt[1][1] or
                                    ((self.custom_fields and
                                      elt[0] in self.custom_fields.keys() and
                                      self.custom_fields[elt[0]][0]) and
                                     self.custom_fields[elt[0]][1]) or
                                    labels[elt[0]])
                                   for elt in self.fields_properties()
                                   if (not self.excluded_fields or
                                       not (elt[0] in self.excluded_fields.keys() and
                                            self.excluded_fields[elt[0]]))]))
        return change_fields

    def email_header_fields(self):
        labels = TicketSystem(self.ticket.env).get_ticket_field_labels()
        header_fields = [('Ticket Creator', 'reporter'), ('Ticket Initiator', 'company')]
        header_fields.extend([(elt[1][1] or ((self.custom_fields and elt[0] in self.custom_fields.keys() and self.custom_fields[elt[0]][0]) and self.custom_fields[elt[0]][1]) or labels[elt[0]], elt[0]) for elt in self.ticketbox_fields() if (not self.excluded_fields or not (elt[0] in self.excluded_fields.keys() and self.excluded_fields[elt[0]]))])
        return header_fields

    def email_change_fields(self):
        change_fields = self.ticket_change_fields().copy()
        del change_fields['submitcomment']
        return change_fields


class EFR_UI(Ticket_UI):

    severity_tip = {'select': 'Enter the code giving the severity of the problem',
                    'Type 0': 'Safety impact',
                    'Type 1A': 'Significant functional failure',
                    'Type 1B': 'Non-significant functional failure',
                    'Type 2': 'Non-functional fault',
                    'Type 3A': 'Significant deviation from plans and standards',
                    'Type 3B': 'Non-significant deviation from plans and standards',
                    'Type 4': 'All other types of problems',
                    'Type 5': 'Non-compliance with certification requirements'}

    def __init__(self, component, ticket):
        super(EFR_UI, self).__init__(component, ticket)
        self.blockedby_label = 'Child DOC(s)/ECR(s)' if 'DOC' in self.ticket_types else 'Child ECR(s)'

    def ticketbox_fields(self, perm=None):
        # name, (attrs,label,tip)
        if not self.ticket.exists:
            skills = self.ticket.env.config.get('ticket-custom', 'skill.options').split('|')
            if 'SYS' in skills:
                return [('type', ([], 'Ticket Type', '')),
                        ('phase', ([], '', '')),
                        ('severity', ([], '', '')),
                        ('document', ([], 'Baseline tag', '')),
                        ('milestone', ([], '', '')),
                        ('keywords', ([], '', ''))]
            else:
                return [('type', ([], 'Ticket Type', '')),
                        ('phase', ([], '', '')),
                        ('severity', ([], '', '')),
                        ('skill', ([], '', '')),
                        ('document', ([], 'Baseline tag', '')),
                        ('milestone', ([], '', '')),
                        ('keywords', ([], '', ''))]
        else:
            return [('phase', ([], '', '')),
                    ('severity', ([], '', '')),
                    ('blockedby', ([], self.blockedby_label, '')),
                    ('document', ([], 'Baseline tag', '')),
                    ('milestone', ([], '', '')),
                    ('keywords', ([], '', ''))]

    def fields_properties(self, perm=None):
        if not self.ticket.exists:
            skills = self.ticket.env.config.get('ticket-custom', 'skill.options').split('|')
            if 'SYS' in skills:
                return [('owner', ([], 'Assign to', self.owner_tip)),
                        ('type', ([('onchange', self.on_ticket_type_change)], 'Ticket Type', self.type_tip)),
                        ('phase', ([], '', 'Enter the phase corresponding to the problem detection')),
                        ('severity', ([], '', self.severity_tip)),
                        ('document', ([('readonly', 'true'), ('style', 'background-color:#f4f4f4')], '', 'Baseline tag on which an EFR is created')),
                        ('milestone', ([('style', 'min-width: 160px;')], '', 'The planned review or CI version for the resolution of this EFR')),
                        ('keywords', ([], '', self.keywords_tip))]
            else:
                return [('owner', ([], 'Assign to', self.owner_tip)),
                        ('type', ([('onchange', self.on_ticket_type_change)], 'Ticket Type', self.type_tip)),
                        ('phase', ([], '', 'Enter the phase corresponding to the problem detection')),
                        ('severity', ([], '', self.severity_tip)),
                        ('skill', ([('onchange', self.on_skill_change)], '', self.skill_tip)),
                        ('document', ([('readonly', 'true'), ('style', 'background-color:#f4f4f4')], '', 'Baseline tag on which an EFR is created')),
                        ('milestone', ([('style', 'min-width: 160px;')], '', 'The planned review or CI version for the resolution of this EFR')),
                        ('keywords', ([], '', self.keywords_tip))]
        else:
            return [('phase', ([], '', 'Enter the phase corresponding to the problem detection')),
                    ('severity', ([], '', self.severity_tip)),
                    ('blockedby', ([], self.blockedby_label, 'TRAC ticket numbers. Ex: 3,6,8 Those tickets need to be closed before this ticket can be closed.')),
                    ('document', ([('readonly', 'true'), ('style', 'background-color:#f4f4f4')], 'Baseline tag', 'Baseline tag on which an EFR is created')),
                    ('milestone', ([('style', 'min-width: 160px;')], '', 'The planned review or CI version for the resolution of this EFR')),
                    ('keywords', ([], '', self.keywords_tip))]


class ECR_UI(Ticket_UI):

    ecrtype_tip = {'select': '',
                   'Evolution': 'This ECR is in response to a change request',
                   'Problem Report': 'This ECR is in response to a failure report'}

    def __init__(self, component, ticket):
        super(ECR_UI, self).__init__(component, ticket)
        self.on_ecrtype_change = 'on_ecrtype_change();'
        self.custom_fields = {'blocking': (ticket['ecrtype'] == 'Problem Report', 'Parent EFR(s)')}
        self.blockedby_label = 'Child DOC(s)/ECR(s)' if 'DOC' in self.ticket_types else 'Child ECR(s)'

    def ticketbox_fields(self, perm=None):
        # name, (attrs,label,tip)
        if not self.ticket.exists:
            return [('type', ([], 'Ticket Type', '')),
                    ('ecrtype', ([], '', '')),
                    ('blocking', ([], '', '')),  # Label set in js: on_ecrtype_change()
                    ('blockedby', ([], self.blockedby_label, '')),
                    ('skill', ([], '', '')),
                    ('document', ([], 'Baseline tag', '')),
                    ('milestone', ([], '', '')),
                    ('keywords', ([], '', ''))]
        else:
            return [('ecrtype', ([], '', '')),
                    ('blocking', ([], '', '')),  # Label set in js: on_ecrtype_change()
                    ('blockedby', ([], self.blockedby_label, '')),
                    ('document', ([], 'Baseline tag', '')),
                    ('milestone', ([], '', '')),
                    ('keywords', ([], '', '')),
                    ('requirements', ([], '', ''))]

    def fields_properties(self, perm=None):
        if not self.ticket.exists:
            return [('owner', ([], 'Assign to', self.owner_tip)),
                    ('type', ([('onchange', self.on_ticket_type_change)], 'Ticket Type', self.type_tip)),
                    ('ecrtype', ([('onchange', self.on_ecrtype_change)], '', self.ecrtype_tip)),
                    ('blocking', ([], '', '')),  # Label set in js: on_ecrtype_change()
                    ('blockedby', ([], self.blockedby_label, 'TRAC ticket numbers. Ex: 3,6,8 Those tickets need to be closed before this ticket can be closed.')),
                    ('skill', ([('onchange', self.on_skill_change)], '', self.skill_tip)),
                    ('document', ([('readonly', 'true'), ('style', 'background-color:#f4f4f4')], '', 'Baseline tag on which an ECR is created')),
                    ('milestone', ([('style', 'min-width: 160px;')], '', 'The planned review or CI version for the implementation of this ECR')),
                    ('keywords', ([], '', self.keywords_tip))]
        else:
            return [('ecrtype', ([('onchange', self.on_ecrtype_change)], '', self.ecrtype_tip)),
                    ('blocking', ([], '', '')),  # Label set in js: on_ecrtype_change()
                    ('blockedby', ([], self.blockedby_label, 'TRAC ticket numbers. Ex: 3,6,8 Those tickets need to be closed before this ticket can be closed.')),
                    ('document', ([('readonly', 'true'), ('style', 'background-color:#f4f4f4')], 'Baseline tag', 'Baseline tag on which an ECR is created')),
                    ('milestone', ([('style', 'min-width: 160px;')], '', 'The planned review or CI version for the implementation of this ECR')),
                    ('keywords', ([], '', self.keywords_tip)),
                    ('requirements', ([], '', "List of requirements IDs impacted by this ECR - each requirement ID must be in a line of it's own. Leave it empty if not applicable."))]


class RF_UI(Ticket_UI):

    def ticketbox_fields(self, perm=None):
        # name, (attrs,label,tip)
        if not self.ticket.exists:
            return [('type', ([], 'Ticket Type', '')),
                    ('document', ([], 'Document tag', '')),
                    ('milestone', ([], '', '')),
                    ('parent', ([], 'Parent Ticket', '')),
                    ('pdffile', ([], '', ''))]
        else:
            return [('document', ([], 'Document tag', '')),
                    ('milestone', ([], '', '')),
                    ('parent', ([], 'Parent Ticket', '')),
                    ('pdffile', ([], '', ''))]

    def fields_properties(self, perm=None):
        if not self.ticket.exists:
            return [('owner', ([('onchange', self.on_owner_change)], 'Assign to', self.owner_tip)),
                    ('type', ([('onchange', self.on_ticket_type_change)], 'Ticket Type', self.type_tip)),
                    ('document', ([('readonly', 'true'), ('style', 'background-color:#f4f4f4')], '', 'Document tag on which a RF is created')),
                    ('milestone', ([('style', 'min-width: 160px;')], '', 'The planned review of the document tag')),
                    ('parent', ([], 'Parent Ticket', 'Enter the Parent Ticket Id (DOC/ECM/FEE),  in the form: #NNN (TRAC ticket id)')),
                    ('pdffile', ([], '', 'Enter the name of the PDF file associated with the document (not applicable for an EOC or BITSTREAM)'))
                    ]
        else:
            return [('milestone', ([('style', 'min-width: 160px;')], '', 'The planned review of the document tag')),
                    ('parent', ([], 'Parent Ticket', 'Enter the Parent Ticket Id (DOC/ECM/FEE),  in the form: #NNN (TRAC ticket id)'))]


class MOM_UI(Ticket_UI):

    def __init__(self, component, ticket):
        super(MOM_UI, self).__init__(component, ticket)
        self.on_momtype_click = 'on_momtype_click();'
        self.on_momtype_change = ('on_momtype_change("%s", "%s", "%s");' %
                                  (self.ticket_type,
                                   self.program_name,
                                   self.urltracbrowse))
        # Conditional exclusion of fields for created tickets
        # for ticketbox fields or properties fields
        self.excluded_fields = {'milestonetag': ticket['momtype'] in
                                ('Progress', 'Audit'),
                                'milestone': ticket['momtype'] != 'Progress',
                                'qmsref': ticket['momtype'] != 'Audit'}

    def ticketbox_fields(self, perm=None):
        # name, (attrs,label,tip)
        if not self.ticket.exists:
            return [('type', ([], 'Ticket Type', '')),
                    ('skill', ([], '', '')),
                    ('momtype', ([], '', '')),
                    ('milestonetag', ([], '', '')),
                    ('milestone', ([], '', '')),
                    ('keywords', ([], '', '')),
                    ('duedate', ([], 'Meeting Date', '')),
                    ('qmsref', ([], '', ''))]
        else:
            return [('milestonetag', ([], '', '')),
                    ('milestone', ([], '', '')),
                    ('keywords', ([], '', '')),
                    ('duedate', ([], 'Meeting Date', '')),
                    ('qmsref', ([], '', ''))]

    def fields_properties(self, perm=None):
        if not self.ticket.exists:
            return [('owner', ([], 'Assign to', self.owner_tip)),
                    ('type', ([('onchange', self.on_ticket_type_change)], 'Ticket Type', self.type_tip)),
                    ('skill', ([('onchange', self.on_skill_change)], '', self.skill_tip)),
                    ('momtype', ([('onclick', self.on_momtype_click), ('onchange', self.on_momtype_change)], '', {'CCB': 'Change Control Board Minutes of Meeting', 'Progress': 'Progress Minutes of Meeting', 'Review': 'Formal Review Minutes of Meeting', 'Audit': 'Audit Minutes of Meeting'})),
                    ('milestonetag', ([('style', 'min-width: 160px;'), ('onchange', self.on_milestonetag_change)], '', '')),  # See ticket.html for the tip
                    ('milestone', ([('style', 'min-width: 160px;')], '', 'The planned review')),
                    ('keywords', ([], '', self.keywords_tip)),
                    ('duedate', ([('onchange', self.on_duedate_change), ('style', 'width: 50%')], 'Meeting Date', 'The scheduled date of the Meeting')),
                    ('qmsref', ([], '', 'When the MOM is registered in the QMS,  enter its QMS reference (EN-X.Y-ZZZ) (TRAC will provide a direct link to the document)'))]
        else:
            return [('keywords', ([], '', self.keywords_tip)),
                    ('duedate', ([('onchange', self.on_duedate_change), ('style', 'width: 50%')], 'Meeting Date', 'The effective date of the Meeting')),
                    ('qmsref', ([], '', 'When the MOM is registered in the QMS,  enter its QMS reference (EN-X.Y-ZZZ) (TRAC will provide a direct link to the document)'))]


class RISK_UI(Ticket_UI):

    probability_tip = {'select': '',
                       'VH': 'Very High',
                       'H': 'High',
                       'M': 'Medium',
                       'L': 'Low',
                       'VL': 'Very Low'}
    impact_tip = {'select': '',
                  'VH': 'Very High',
                  'H': 'High',
                  'M': 'Medium',
                  'L': 'Low',
                  'VL': 'Very Low'}

    def __init__(self, component, ticket):
        super(RISK_UI, self).__init__(component, ticket)
        self.on_evaluation_change = 'on_evaluation_change();'

    def ticketbox_fields(self, perm=None):
        # name, (attrs,label,tip)
        if not self.ticket.exists:
            return [('type', ([], 'Ticket Type', '')),
                    ('skill', ([], '', '')),
                    ('milestone', ([], '', '')),
                    ('keywords', ([], '', ''))]
        else:
            return [('milestone', ([], '', '')),
                    ('keywords', ([], '', '')),
                    ('probability', ([], '', '')),
                    ('impact', ([], '', '')),
                    ('rating', ([], '', '')),
                    ('cost', ([], '', ''))]

    def fields_properties(self, perm=None):
        if not self.ticket.exists:
            return [('owner', ([], 'Assign to', self.owner_tip)),
                    ('type', ([('onchange', self.on_ticket_type_change)], 'Ticket Type', self.type_tip)),
                    ('skill', ([('onchange', self.on_skill_change)], '', self.skill_tip)),
                    ('milestone', ([('style', 'min-width: 160px;')], '', 'Milestone affected by this risk')),
                    ('keywords', ([], '', self.keywords_tip))]
        else:
            return [('milestone', ([('style', 'min-width: 160px;')], '', 'Milestone affected by this risk')),
                    ('keywords', ([], '', self.keywords_tip)),
                    ('probability', ([('onchange', self.on_evaluation_change)], '', self.probability_tip)),
                    ('impact', ([('onchange', self.on_evaluation_change)], '', self.impact_tip)),
                    ('rating', ([('readonly', 'true'), ('style', 'font-weight:bold;text-align:center;width:90%')], '', '')),
                    ('cost', ([], '', ''))]

    def ticket_change_fields(self):
        change_fields = super(RISK_UI, self).ticket_change_fields()
        change_fields.update({'description': 'Description'})
        return change_fields

    def email_header_fields(self):
        header_fields = super(RISK_UI, self).email_header_fields()
        header_fields.append(('Description', 'description'))
        return header_fields


class AI_UI(Ticket_UI):

    deviationtype_tip = {'select': '', 'NC': u'Non Conformité', 'VA': u"Voie d'amélioration", 'R': 'Remarque'}
    aitype_tip = {'select': '',
                  'Mitigation':
                      '<p><b>Your action to prevent risk before it happens:</b></p>'
                      '<p>makes budget bigger as you include cost of risk</p>'
                      '<p>that did&#39;nt happen and may not happen</p>',
                  'Contingency':
                      '<p><b>Your action to cure risk after it happens:</b></p>'
                      '<p>makes budget smaller but may increase it</p>'
                      '<p>after and if risk happens</p>'}

    def __init__(self, component, ticket):
        super(AI_UI, self).__init__(component, ticket)
        self.on_activity_change = 'on_activity_change();'
        # Conditional exclusion of fields for created tickets
        self.excluded_fields = {'aitype': ticket['activity'] != 'Risk Management',
                                'deviationtype': ticket['activity'] != 'Quality Assurance',
                                'requirement': ticket['activity'] != 'Quality Assurance',
                                'nonconformity': ticket['activity'] != 'Quality Assurance'}

    def ticketbox_fields(self, perm=None):
        # name, (attrs,label,tip)
        if not self.ticket.exists:
            return [('type', ([], 'Ticket Type', '')),
                    ('skill', ([], '', '')),
                    ('activity', ([], '', '')),
                    ('milestone', ([], '', '')),
                    ('keywords', ([], '', '')),
                    ('parent', ([], '', '')),
                    ('duedate', ([], '', '')),
                    ('aitype', ([], '', ''))]
        else:
            return [('activity', ([], '', '')),
                    ('milestone', ([], '', '')),
                    ('keywords', ([], '', '')),
                    ('parent', ([], '', '')),
                    ('duedate', ([], '', '')),
                    ('aitype', ([], '', '')),
                    ('deviationtype', ([], '', '')),
                    ('requirement', ([], '', '')),
                    ('nonconformity', ([], '', ''))]

    def fields_properties(self, perm=None):
        if not self.ticket.exists:
            return [('owner', ([], 'Assign to', self.owner_tip)),
                    ('type', ([('onchange', self.on_ticket_type_change)], 'Ticket Type', self.type_tip)),
                    ('skill', ([('onchange', self.on_skill_change)], '', self.skill_tip)),
                    ('activity', ([('onchange', self.on_activity_change)], '', '')),
                    ('milestone', ([('style', 'min-width: 160px;')], '', 'The milestone when the Action Item has to be completed')),
                    ('keywords', ([], '', 'Enter the Action Item Id as referenced in the MOM (if applicable)')),
                    ('parent', ([], '', 'Enter the Parent Ticket Id (MOM/RISK),  in the form: #NNN (TRAC ticket id)')),
                    ('duedate', ([('onchange', self.on_duedate_change), ('style', 'width:50%')], '', 'The date when the Action Item is scheduled to be completed')),
                    ('aitype', ([], '', self.aitype_tip))]
        else:
            return [('milestone', ([('style', 'min-width: 160px;')], '', 'The milestone when the Action Item has to be completed')),
                    ('keywords', ([], '', 'Enter the Action Item Id as referenced in the MOM (if applicable)')),
                    ('parent', ([], '', 'Enter the Parent Ticket Id (MOM/RISK),  in the form: #NNN (TRAC ticket id)')),
                    ('duedate', ([('onchange', self.on_duedate_change), ('style', 'width:50%')], '', 'The date when the Action Item is scheduled to be completed')),
                    ('aitype', ([], '', self.aitype_tip)),
                    ('deviationtype', ([], '', self.deviationtype_tip)),
                    ('requirement', ([], '', '')),
                    ('nonconformity', ([], '', ''))]

    def ticket_change_fields(self):
        change_fields = super(AI_UI, self).ticket_change_fields()
        change_fields.update({'description': 'Description'})
        return change_fields

    def email_header_fields(self):
        header_fields = super(AI_UI, self).email_header_fields()
        header_fields.append(('Description', 'description'))
        return header_fields


class PRF_UI(Ticket_UI):

    def __init__(self, component, ticket):
        super(PRF_UI, self).__init__(component, ticket)
        milestone_filter_string = self.ticket.env.config.get('artusplugin', 'milestone_filter').strip()
        # Conditional exclusion of fields for created tickets
        # for ticketbox fields or properties fields
        self.excluded_fields = {'milestone': milestone_filter_string == ''}
        
    def ticketbox_fields(self, perm=None):
        # name, (attrs,label,tip)
        if not self.ticket.exists:
            return [('type', ([], 'Ticket Type', '')),
                    ('document', ([], 'Parent Tag', '')),
                    ('milestone', ([], '', '')),
                    ('parent', ([], 'Parent Ticket', '')),
                    ('pdffile', ([], 'Parent PDF', ''))]
        else:
            return [('document', ([], 'Parent Tag', '')),
                    ('milestone', ([], '', '')),
                    ('parent', ([], 'Parent Ticket', '')),
                    ('pdffile', ([], 'Parent PDF', ''))]

    def fields_properties(self, perm=None):
        if not self.ticket.exists:
            return [('owner', ([('onchange', self.on_owner_change)], 'Assign to', self.owner_tip)),
                    ('type', ([('onchange', self.on_ticket_type_change)], 'Ticket Type', self.type_tip)),
                    ('document', ([('readonly', 'true'), ('style', 'background-color:#f4f4f4')], '', 'Parent Tag on which a PRF is created')),
                    ('milestone', ([('style', 'min-width: 160px;')], '', 'The planned review of the document tag')),
                    ('parent', ([], 'Parent Ticket', 'Enter the Parent Ticket Id (DOC/ECM/FEE),  in the form: #NNN (TRAC ticket id)')),
                    ('pdffile', ([('style', 'min-width:240px;')], 'Parent PDF', 'Enter the name of the PDF file associated with the Parent Ticket (not applicable for an EOC or BITSTREAM)'))]
        else:
            return [('milestone', ([('style', 'min-width: 160px;')], '', 'The planned review of the document tag')),
                    ('parent', ([], 'Parent Ticket', 'Enter the Parent Ticket Id (DOC/ECM/FEE),  in the form: #NNN (TRAC ticket id)'))]


class MEMO_UI(Ticket_UI):

    def ticketbox_fields(self, perm=None):
        # name, (attrs,label,tip)
        if not self.ticket.exists:
            return [('type', ([], 'Ticket Type', '')),
                    ('skill', ([], '', '')),
                    ('document', ([], 'Baseline tag', '')),
                    ('keywords', ([], '', ''))]
        else:
            return [('document', ([], 'Baseline tag', '')),
                    ('keywords', ([], '', ''))]

    def fields_properties(self, perm=None):
        if not self.ticket.exists:
            return [('owner', ([], 'Assign to', self.owner_tip)),
                    ('type', ([('onchange', self.on_ticket_type_change)], 'Ticket Type', self.type_tip)),
                    ('skill', ([('onchange', self.on_skill_change)], '', self.skill_tip)),
                    ('document', ([('readonly', 'true'), ('style', 'background-color:#f4f4f4')], '', 'Baseline tag on which a MEMO is created')),
                    ('keywords', ([], '', self.keywords_tip))]
        else:
            return [('document', ([('readonly', 'true'), ('style', 'background-color:#f4f4f4')], 'Baseline tag', 'Baseline tag on which a MEMO is created')),
                    ('keywords', ([], '', self.keywords_tip))]

    def ticket_change_fields(self):
        change_fields = super(MEMO_UI, self).ticket_change_fields()
        change_fields.update({'description': 'Description'})
        return change_fields

    def email_header_fields(self):
        header_fields = super(MEMO_UI, self).email_header_fields()
        header_fields.append(('Description', 'description'))
        return header_fields


class ECM1_UI(Ticket_UI):

    legacy = True

    def ticketbox_fields(self, perm=None):
        # name, (attrs,label,tip)
        if not self.ticket.exists:
            return [('type', ([], 'Ticket Type', '')),
                    ('skill', ([], '', '')),
                    ('document', ([], 'Baseline Tag', '')),
                    ('keywords', ([], '', ''))]
        else:
            return [('document', ([], 'Baseline Tag', '')),
                    ('keywords', ([], '', ''))]

    def fields_properties(self, perm=None):
        if not self.ticket.exists:
            return [('owner', ([], 'Assign to', self.owner_tip)),
                    ('type', ([('onchange', self.on_ticket_type_change)], 'Ticket Type', self.type_tip)),
                    ('skill', ([('onchange', self.on_skill_change)], '', self.skill_tip)),
                    ('document', ([('readonly', 'true'), ('style', 'background-color:#f4f4f4'), ('size', '50')], '', 'Baseline tag on which an ECM is created')),
                    ('keywords', ([('size', '40')], '', self.keywords_tip))]
        else:
            return [('document', ([('readonly', 'true'), ('style', 'background-color:#f4f4f4'), ('size', '50')], 'Baseline tag', 'Baseline tag on which an ECM is created')),
                    ('keywords', ([('size', '40')], '', self.keywords_tip))]

    def ticket_change_fields(self):
        change_fields = super(ECM1_UI, self).ticket_change_fields()
        change_fields.update({'description': 'Description'})

        return change_fields

    def email_header_fields(self):
        header_fields = super(ECM1_UI, self).email_header_fields()
        header_fields.append(('Description', 'description'))

        return header_fields


class ECM2_UI(Ticket_UI):

    legacy = False

    ecmtype_tip = {'select': '',
                   'Technical Note': 'This ECM type is used for engaging in a technical discussion with the customer',
                   'Document Delivery': 'This ECM type is used for formally sending documents to the customer'}

    def __init__(self, component, ticket):
        super(ECM2_UI, self).__init__(component, ticket)
        self.on_ecmtype_change = 'on_ecmtype_change("%s", "%s");' % (
            ticket['type'], self.program_name)
        self.on_fromecm_change = 'on_fromecm_change("%s", "%s");' % (
            ticket['type'], self.program_name)
        self.on_sourcefile_change = 'on_sourcefile_change();'
        self.on_pdffile_change = 'on_pdffile_change();'
        self.on_field_change = 'on_field_change(["keywords"]);'
        self.single_fields = ['carboncopy', 'sourceurl']
        self.grouped_fields = ['sourcefile', 'pdffile']

    def ticketbox_fields(self, perm=None):
        # name, (attrs,label,tip)
        if not self.ticket.exists:
            ticketbox_fields = [('type', ([], 'Ticket Type', '')),
                                ('ecmtype', ([], '', '')),
                                ('fromecm', ([], '', '')),
                                ('milestone', ([], '', '')),
                                ('keywords', ([], '', ''))]
        else:
            ticketbox_fields = [('ecmtype', ([], '', '')),
                                ('milestone', ([], '', '')),
                                ('keywords', ([], '', ''))]
            if self.ticket['ecmtype'] == 'Technical Note':
                ticketbox_fields.extend([('document', ([], 'ECM Tag', ''))])
            ticketbox_fields.extend([('sourceurl', ([], '', '')),
                                     ('sourcefile', ([], '', '')),
                                     ('pdffile', ([], '', ''))])
        return ticketbox_fields

    def fields_properties(self, perm=None):
        if not self.ticket.exists:
            fields_properties = [('type', ([('onchange', self.on_ticket_type_change)],
                                           'Ticket Type',
                                           self.type_tip)),
                                 ('ecmtype', ([('onchange', self.on_ecmtype_change)],
                                              '',
                                              self.ecmtype_tip)),
                                 ('fromecm', ([('style', 'min-width: 160px;'),
                                              ('onchange', self.on_fromecm_change)],
                                              '',
                                              'Choose between creating a new Technical Note or only a new version of an existing one')),
                                 ('milestone', ([('style', 'min-width: 160px;')],
                                                '',
                                                'The planned review')),
                                 ('keywords', ([('size', '40')], '', self.keywords_tip))]
        else:
            fields_properties = [('milestone', ([('style', 'min-width: 160px;')],
                                                '',
                                                'The planned review'))]
            if self.ticket['status'] == '01-assigned_for_edition':
                fields_properties.extend([('keywords', ([('size', '40'), ('onchange', self.on_field_change)],
                                                        '',
                                                        self.keywords_tip))])
            else:
                fields_properties.extend([('keywords', ([('size', '40'), ('readonly', 'true'), ('style', 'background-color:#f4f4f4')],
                                                        '',
                                                        self.keywords_tip))])

            workflow = self.component.action_controllers
            if workflow[0].get_ticket_wf(self.ticket).get_status(self.ticket) == '05-assigned_for_sending':
                fields_properties.extend([('fromname', ([('size', '40'), ('readonly', 'true'), ('style', 'background-color:#f4f4f4')],
                                                        '',
                                                        'Syntax: First Name NAME')),
                                          ('toname', ([('size', '40')],
                                                      '',
                                                      'Syntax: First Name NAME')),
                                          ('fromemail', ([('size', '40'), ('readonly', 'true'), ('style', 'background-color:#f4f4f4')],
                                                         '',
                                                         'Syntax: username@domain')),
                                          ('toemail', ([('size', '40')],
                                                       '',
                                                       'Syntax: username@domain')),
                                          ('fromphone', ([('size', '40')],
                                                         '',
                                                         'Syntax: +33 (0) XXX XXX XXX')),
                                          ('tophone', ([('size', '40')],
                                                       '',
                                                       'Syntax: +33 (0) XXX XXX XXX')),
                                          ('carboncopy', ([],
                                                          '',
                                                          'Syntax: First Name NAME (username@domain)'))])
            fields_properties.extend([('sourceurl', ([('readonly', 'true'),
                                                      ('style', 'background-color:#f4f4f4;width:90%')],
                                                     '',
                                                     'The location of the document folder in the repository')),
                                      ('sourcefile', ([('onchange', self.on_sourcefile_change)],
                                                      '',
                                                      'The name of the source file')),
                                      ('pdffile', ([('onchange', self.on_pdffile_change)],
                                                   '',
                                                   'The name of the PDF file'))])
        return fields_properties

    def required_fields(self):
        return ['fromname', 'fromemail', 'fromphone', 'toname', 'toemail', 'tophone']

    def ticket_change_fields(self):
        change_fields = super(ECM2_UI, self).ticket_change_fields()
        change_fields.update({'document': 'Document Tag'})
        change_fields.update({'keywords': 'Keywords'})
        change_fields.update({'fromname': 'From Name'})
        change_fields.update({'toname': 'To Name'})
        change_fields.update({'fromemail': 'From Email*'})
        change_fields.update({'toemail': 'To Email*'})
        change_fields.update({'fromphone': 'From Phone'})
        change_fields.update({'tophone': 'To Phone'})
        change_fields.update({'carboncopy': 'Carbon Copy'})

        return change_fields

    def email_header_fields(self):
        header_fields = super(ECM2_UI, self).email_header_fields()
        header_fields.append(('Source Url', 'sourceurl'))
        header_fields.append(('Document Tag', 'document'))

        return header_fields


class FEE_UI(Ticket_UI):

    legacy = False

    def __init__(self, component, ticket):
        super(FEE_UI, self).__init__(component, ticket)
        self.on_fromfee_change = 'on_fromfee_change("%s", "%s");' % (
            ticket['type'], self.program_name)
        self.on_evolref_change = 'on_evolref_change("%s", "%s");' % (
            ticket['type'], self.program_name)
        self.on_sourcefile_change = 'on_sourcefile_change();'
        self.on_pdffile_change = 'on_pdffile_change();'
        self.single_fields = ['carboncopy', 'sourceurl', 'items']
        self.grouped_fields = ['sourcefile', 'pdffile']

    def ticketbox_fields(self, perm=None):
        # name, (attrs,label,tip)
        if not self.ticket.exists:
            ticketbox_fields = [('type', ([], 'Type', '')),
                                ('fromfee', ([], 'Maj FEE', '')),
                                ('evolref', ([], 'No Evolution', '')),
                                ('customer', ([], 'Client', '')),
                                ('program', ([], 'Programme', '')),
                                ('application', ([], 'Application', ''))]
        else:
            ticketbox_fields = [('customer', ([], 'Client', '')),
                                ('program', ([], 'Programme', '')),
                                ('application', ([], 'Application', ''))]
            ticketbox_fields.extend([('document', ([], 'Etiquette FEE', ''))])
            ticketbox_fields.extend([('sourceurl', ([], 'Url Source', '')),
                                     ('sourcefile', ([], 'Fichier Source', '')),
                                     ('pdffile', ([], 'Fichier PDF', ''))])
        return ticketbox_fields

    def fields_properties(self, perm=None):
        if not self.ticket.exists:
            fields_properties = [('owner', ([], 'Assigner a', self.owner_tip)),
                                 ('type', ([('onchange', self.on_ticket_type_change)],
                                           'Type de Ticket',
                                           self.type_tip)),
                                 ('fromfee', ([('style', 'min-width: 160px;'),
                                              ('onchange', self.on_fromfee_change)],
                                              'Maj FEE',
                                              'Choose between creating a new Evolution Sheet or only a new version of an existing one')),
                                ('evolref', ([('style', 'min-width: 160px;'),
                                              ('onchange', self.on_evolref_change)],
                                              'No Evolution',
                                              '')),
                                ('customer', ([('style', 'min-width: 160px;')],
                                              'Client',
                                              '')),
                                ('program', ([('style', 'min-width: 160px;')],
                                              'Programme',
                                              '')),
                                ('application', ([('style', 'min-width: 160px;')],
                                              'Application',
                                              ''))]
        else:
            fields_properties = []
            workflow = self.component.action_controllers
            if workflow[0].get_ticket_wf(self.ticket).get_status(self.ticket) == '05-assigned_for_sending':
                fields_properties.extend([('customer', (['style', 'min-width: 160px;'],
                                              'Client',
                                              '')),
                                          ('program', (['style', 'min-width: 160px;'],
                                              'Programme',
                                              '')),
                                          ('application', (['style', 'min-width: 160px;'],
                                              'Application',
                                              ''))])
                fields_properties.extend([('fromname', ([('size', '40'), ('readonly', 'true'), ('style', 'background-color:#f4f4f4')],
                                                        'Nom Expediteur',
                                                        'Syntax: First Name NAME')),
                                          ('toname', ([('size', '40')],
                                                      'Nom Destinataire',
                                                      'Syntax: First Name NAME')),
                                          ('fromemail', ([('size', '40'), ('readonly', 'true'), ('style', 'background-color:#f4f4f4')],
                                                         'Email Expediteur',
                                                         'Syntax: username@domain')),
                                          ('toemail', ([('size', '40')],
                                                       'Email Destinataire',
                                                       'Syntax: username@domain')),
                                          ('fromphone', ([('size', '40')],
                                                         'Tel Expediteur',
                                                         'Syntax: +33 (0) XXX XXX XXX')),
                                          ('tophone', ([('size', '40')],
                                                       'Tel Destinataire',
                                                       'Syntax: +33 (0) XXX XXX XXX')),
                                          ('carboncopy', ([],
                                                          'Copie Carbone',
                                                          'Syntax: First Name NAME (username@domain)'))])
            fields_properties.extend([('sourceurl', ([('readonly', 'true'),
                                                      ('style', 'background-color:#f4f4f4;width:90%')],
                                                     'Url Source',
                                                     'The location of the document folder in the repository')),
                                      ('sourcefile', ([('onchange', self.on_sourcefile_change)],
                                                      'Fichier Source',
                                                      'The name of the source file')),
                                      ('pdffile', ([('onchange', self.on_pdffile_change)],
                                                    'Fichier PDF',
                                                    'The name of the PDF file'))])
        return fields_properties

    def required_fields(self):
        return ['fromname', 'fromemail', 'fromphone', 'toname', 'toemail', 'tophone']

    def ticket_change_fields(self):
        change_fields = super(FEE_UI, self).ticket_change_fields()
        change_fields.update({'document': 'Document Tag'})
        change_fields.update({'fromname': 'From Name'})
        change_fields.update({'toname': 'To Name'})
        change_fields.update({'fromemail': 'From Email*'})
        change_fields.update({'toemail': 'To Email*'})
        change_fields.update({'fromphone': 'From Phone'})
        change_fields.update({'tophone': 'To Phone'})
        change_fields.update({'carboncopy': 'Carbon Copy'})

        return change_fields

    def email_header_fields(self):
        header_fields = super(FEE_UI, self).email_header_fields()
        header_fields.append(('Source Url', 'sourceurl'))
        header_fields.append(('Document Tag', 'document'))

        return header_fields


class DOC_UI(Ticket_UI):

    changetype_tip = {'select': 'The change type applied to the document',
                      'Edition': 'A major change or profound rewrite, as in the case of a functional impact, a review failure, a new development phase, ...',
                      'Revision': 'A minor change, with no functional impact, review acceptance with reservations, typographic or editorial corrections, ...',
                      'Status': 'A change in the status or status index of the document, that is the work flow that the document goes through, for a targeted version, from Draft to Proposed and finally Released',
                      'Version': 'A major or minor change resulting in a new document version for documents with unmanaged skill (eg EXT)'}

    controlcategory_tip = {'select': 'Configuration management processes and activities applied to control life cycle data as required by the determined Design Assurance Level (DAL) for the component',
                           'CC1/HC1': 'Control Category 1. The Higher control category. An ECR is required to create a new version of a Released document',
                           'CC2/HC2': 'Control Category 2. The Lower control category. No ECR is required to create a new version of a Released document',
                           'N/A': 'No control category is defined. No ECR is required to create a new version of a Released document'}

    submittedfor_tip = {'select': 'Following the initial document development and necessary internal check and review processes to ensure adequate quality, the document will be released for external review ultimately leading to its approval',
                        'Approval': 'An agreement or consensus on an activity or output comparing with baselines or contractual requirements by an approver to proceed a next step of activity, or work defined in project plans and procedures',
                        'Review': 'An activity to figure out how well the thing being done is capable of achieving established objectives',
                        'Information': 'Information is meaningful and valuable data that can affect the behaviour, decision, or outcome, and lead to an increase in understanding and decrease in uncertainty'}
    
    @staticmethod
    def sourcetype_tip(env):
        source_types = util.get_prop_values(env, "source_types")
        sourcetype_tip = OrderedDict([("select", "Choosing the document source type often means "
             "choosing the document template and leads to a default setting for the source and PDF files")] +
            [(st, source_types[st].split('||')[6].strip()) for st in source_types])
        return sourcetype_tip

    def __init__(self, component, ticket):
        super(DOC_UI, self).__init__(component, ticket)
        self.on_configurationitem_change = 'on_configurationitem_change("%s", "%s");' % (
            ticket['type'], self.program_name)
        self.on_changetype_change = 'on_changetype_change("%s", "%s");' % (
            ticket['type'], self.program_name)
        self.on_fromversion_change = 'on_fromversion_change("%s", "%s");' % (
            ticket['type'], self.program_name)
        self.on_versionsuffix_change = 'on_versionsuffix_change("%s", "%s");' % (
            ticket['type'], self.program_name)
        self.on_sourcefile_change = 'on_sourcefile_change();'
        self.on_pdffile_change = 'on_pdffile_change();'
        self.single_fields = ['sourceurl']
        self.grouped_fields = ['sourcefile', 'pdffile']

    def ticketbox_fields(self, perm=None):
        # name, (attrs,label,tip)
        if not self.ticket.exists:
            ticketbox_fields = [('type', ([], 'Ticket Type', '')),
                                ('skill', ([], '', '')),
                                ('configurationitem', ([], '', '')),
                                ("sourcetype", ([], "", "")),
                                ("pdfsigned", ([], "", "")),
                                ("independence", ([], "", "")),
                                ('controlcategory', ([], '', '')),
                                ('submittedfor', ([], '', '')),
                                ('changetype', ([], '', '')),
                                ('fromversion', ([], '', '')),
                                ('versionsuffix', ([], '', '')),
                                ('blocking', ([], 'Parent Ticket(s)', '')),
                                ('milestone', ([], '', ''))]     
        else:
            ticketbox_fields = [("sourcetype", ([], "", "")),
                                ("pdfsigned", ([], "", "")),
                                ("independence", ([], "", "")),
                                ('controlcategory', ([], '', '')),
                                ('submittedfor', ([], '', '')),
                                ('blocking', ([], 'Parent Ticket(s)', '')),
                                ('milestone', ([], '', '')),
                                ('document', ([], 'Document Tag', '')),
                                ('parent', ([], 'CCB MOM', '')),
                                ('sourceurl', ([], '', '')),
                                ('sourcefile', ([], '', '')),
                                ('pdffile', ([], '', ''))]

        return ticketbox_fields

    def fields_properties(self, perm=None):
        if not self.ticket.exists:
            fields_properties = [('type', ([('onchange', self.on_ticket_type_change)],
                                           'Ticket Type',
                                           self.type_tip)),
                                 ('skill', ([('onchange', self.on_skill_change)],
                                            '',
                                            self.skill_tip)),
                                 ('configurationitem', ([('style', 'min-width: 160px;'),
                                                         ('onchange', self.on_configurationitem_change)],
                                                        '',
                                                        'The document Configuration Item name')),
                                 ('sourcetype', ([], "", self.sourcetype_tip(self.ticket.env))),
                                 ('pdfsigned', ([], "", "If the PDF file is to be digitally signed, spaces for signatures must exist on the first page of the document")),
                                 ('independence',([], "", "The reviewer of a document with independence set to on cannot be amongst the writers of the document")),
                                 ('controlcategory', ([],
                                                      '',
                                                      self.controlcategory_tip)),
                                 ('submittedfor', ([],
                                                   '',
                                                   self.submittedfor_tip)),
                                 ('changetype', ([('onchange', self.on_changetype_change)],
                                                 '',
                                                 self.changetype_tip)),
                                 ('fromversion', ([('style', 'min-width: 160px;'),
                                                  ('onchange', self.on_fromversion_change)],
                                                  '',
                                                  'The document version on which the change is applied')),
                                 ('versionsuffix', ([('readonly', 'true'),
                                                     ('style', 'background-color:#f4f4f4'),
                                                     ('onchange', self.on_versionsuffix_change)],
                                                    '',
                                                    'The version suffix that will be appended to the Configuration Item name')),
                                 ('blocking', ([('style', 'width:90%')],
                                               'Parent Ticket(s)',
                                               '')),  # See ticket.html for the tip
                                 ('milestone', ([('style', 'min-width: 160px;')],
                                                '',
                                                'The planned review for releasing this document version'))]
        else:
            fields_properties = []
            if perm and 'TRAC_ADMIN' in perm:
                fields_properties.extend(
                    [('independence', ([], 
                                       '',
                                       'The reviewer of a document with independence set to on cannot be amongst the writers of the document')),
                     ('controlcategory', ([],
                                          '',
                                          self.controlcategory_tip))])
            fields_properties.extend(
                [('submittedfor', ([],
                                   '',
                                   self.submittedfor_tip)),
                 ('blocking', ([('style', 'width:90%')],
                               'Parent Ticket(s)',
                               '')),
                 ('milestone', ([('style', 'min-width: 160px;')],
                                '',
                                'The planned review for releasing this document version')),
                 ('document', ([('readonly', 'true'),
                                ('style', 'background-color:#f4f4f4;width:90%')],
                               'Document Tag',
                               '')),
                 ('parent', ([('style', 'width:90%')],
                             'CCB MOM',
                             '')),  # See ticket.html for the tip
                 ('sourceurl', ([('readonly', 'true'),
                                 ('style', 'background-color:#f4f4f4;width:90%')],
                                '',
                                'The location of the document folder in the repository')),
                 ('sourcefile', ([('onchange', self.on_sourcefile_change)],
                                 '',
                                 '')),
                 ('pdffile', ([('onchange', self.on_pdffile_change)],
                                              '',
                                              ''))]
            )
        
        return fields_properties

    def ticket_change_fields(self):
        change_fields = super(DOC_UI, self).ticket_change_fields()
        change_fields.update({
            'independence': 'Independence',
            'controlcategory': 'Control Category',
            'submittedfor': 'Submitted for',
            'document': 'Document Tag'})
        return change_fields

    def email_header_fields(self):
        header_fields = super(DOC_UI, self).email_header_fields()
        header_fields.append(('Source Url', 'sourceurl'))
        header_fields.append(('Document Tag', 'document'))
        return header_fields


class ArtusModule(Component):
    """Customizations of ticket processing."""

    implements(ITemplateStreamFilter, IRequestFilter, IRequestHandler, INavigationContributor, ITemplateProvider, IPropertyRenderer)

    action_controllers = OrderedExtensionsOption('ticket', 'workflow',
                                                 ITicketActionController,
                                                 default='ConfigurableTicketWorkflow',
                                                 include_missing=False,
                                                 doc="""Ordered list of workflow controllers to use for ticket actions (''since 0.11'').""")

    def __init__(self):
        self._reports = {'ECRs impacting same requirements': '24', 'Action Items by Due Date': '27', 'List of requirements impacted by ECRs': '51'}

        self._rating_rendering = {'bgcolor': {'G': '#03fd00', 'A': '#ff9a00', 'R': '#ff0100'},
                                  'color': {'G': 'black', 'A': 'black', 'R': 'white'}}

        self._activity = {'MOM': {'CCB': 'Configuration Management',
                                  'Progress': 'Project Monitoring and Control',
                                  'Review': 'Quality Assurance',
                                  'Audit': 'Quality Assurance'},
                          'RISK': 'Risk Management'}

        self._ticket_types = [t.name for t in Type.select(self.env)]

        # Translation
        from pkg_resources import resource_filename  # @UnresolvedImport
        from artusplugin import add_domain
        try:
            locale_dir = resource_filename(__name__, 'locale')
        except KeyError:
            pass  # no locale directory in plugin if Babel is not installed
        else:
            add_domain(self.env.path, locale_dir)
        
        def to_non_breaking_hyphen(stream):
            for mark, (kind, data, pos) in stream:
                if mark and kind is TEXT:
                    yield mark, (kind, data.replace('-', u"\u2011"), pos)
                else:
                    yield mark, (kind, data, pos)

        self.to_non_breaking_hyphen = to_non_breaking_hyphen

        program_data = util.get_program_data(self.env)
        self.trac_env_name = program_data['trac_env_name']
        self.program_name = program_data['program_name']
        translation_file = self.env.config.get('artusplugin', 'translation_file')
        meggitt_translation = configparser.RawConfigParser(delimiters=('='))
        meggitt_translation.optionxform = lambda option: option
        meggitt_translation.read(translation_file)
        self.url_translation = dict(meggitt_translation.items('url-translation'))
        url_regexp_list = []
        for url in self.url_translation:
            url_regexp_list.append('(%s)' % url.replace('/', '\/').replace('.', '\.'))
        self.url_regexp = '(?:' + '|'.join(url_regexp_list) + ')'

    # ITemplateStreamFilter

    def filter_stream(self, req, method, filename, stream, data):
        """ The modifications applied to the TRAC ticket form """

        add_script(req, 'spin.min.js')
        add_script(req, 'wgxpath.install.js')

        my_script_list = glob.glob('%s/htdocs/stamped/artus_*' % os.path.dirname(os.path.realpath(__file__)))
        if len(my_script_list) != 1:
            raise TracError(_("More than one artus.js script or none."))
        else:
            add_script(req, 'artusplugin/stamped/%s' % os.path.basename(my_script_list[0]))
        add_stylesheet(req, 'artusplugin/artus.css')
        Chrome(self.env).add_jquery_ui(req)
        
        if req.locale is not None and str(req.locale) != 'en_US':
            my_script_list = glob.glob('%s/htdocs/stamped/messages/%s_*' %
                                       (os.path.dirname(os.path.realpath(__file__)), req.locale))
            if len(my_script_list) != 1:
                raise TracError(_("More than one %(locale)s.js script or none.", locale=req.locale))
            else:
                add_script(req, 'artusplugin/stamped/messages/%s' % os.path.basename(my_script_list[0]))

        default_skill = self.env.config.get('artusplugin', 'default_skill', 'SYS')
        unmanaged_skills = self.env.config.get('artusplugin', 'unmanaged_skills', 'EXT')

        ct_options = self.env.config.get('ticket-custom', 'changetype.options')
        options = [option.strip() for option in ct_options.split('|')]
        ct_values = [(option, DOC_UI.changetype_tip[option]) for option in options]

        # Dotclear
        dc_url = self.env.config.get('artusplugin', 'dc_url')

        add_script_data(req, g_default_skill=default_skill, g_unmanaged_skills=unmanaged_skills, g_changetypes=ct_values, g_dc_url=dc_url)

        # Sometimes we get a 'maximal recursion depth limit exceeded' with the default value (1000)
        sys.setrecursionlimit(2000)

        add_script_data(req, g_trac_env_name=self.trac_env_name, g_program_name=self.program_name)

        if filename == "ticket.html":

            stream = self._filter_ticket_stream(req, method, filename, stream, data)

        elif filename == "ticket_delete.html":

            stream = self._filter_ticket_delete_stream(req, method, filename, stream, data)

        elif filename == "browser.html":

            stream = self._filter_browser_stream(req, method, filename, stream, data)

        elif filename == "attachment.html" and data['mode'] == 'view' and data['attachment'].parent_realm == 'ticket':

            stream = self._filter_attachment_stream(req, method, filename, stream, data)

        return stream

    def _filter_ticket_stream(self, req, method, filename, stream, data):

        # get ticket type
        trac_id = req.args.get('id')
        if trac_id:
            ticket = Ticket(self.env, trac_id)
            ticket_type = ticket['type']
        else:
            ticket = None
            ticket_type = None

        # new ticket! get type from request (defined at this stage)
        if ticket_type is None:
            ticket_type = req.args.get('type')

        add_script_data(req, g_ticket_type=ticket_type, g_trac_id=trac_id)
        dc_url = self.env.config.get('artusplugin', 'dc_url')

        if not trac_id:
            # Ticket in creation

            if ticket_type in ('RF', 'PRF'):

                # Type help
                if ticket_type == 'PRF':
                    stream |= Transformer('//select[@id="field-owner"]').after(tag.img(id_="type-help", src_="/htdocs/help-mini.jpg"))
                    stream |= Transformer('//img[@id="type-help"]').attr(
                        "style", "vertical-align:middle;").attr(
                        "data-tooltip-content", "#owner_help").attr(
                        "class", "tooltip")

                # A button is added in order to select the document
                urltracbrowse = self.env.base_url + '/browser' + (req.args.get('documenturl') or '/tags')
                urltracbrowse = util.url_add_params(urltracbrowse, [('caller', 't%s' % ticket_type)])
                stream |= Transformer('//*[@for="field-document"]').replace(
                    tag.input(value_="Document tag:",
                              name_='doc_select',
                              type_='button',
                              onclick_='location.href="%s"' % urltracbrowse,
                              title='%s' % urltracbrowse))

            elif ticket_type in ('EFR', 'ECR', 'MEMO'):

                # A button is added in order to select the baseline
                urltracbrowse = self.env.base_url + '/browser' + (req.args.get('documenturl') or '/tags')
                urltracbrowse = util.url_add_params(urltracbrowse, [('caller', 't%s' % ticket_type)])
                stream |= Transformer('//*[@for="field-document"]').replace(
                    tag.input(value_="Baseline tag:",
                              name_='doc_CI_select',
                              type_='button',
                              onclick_='location.href="%s"' % urltracbrowse,
                              title='%s' % urltracbrowse))

                skills = self.env.config.get('ticket-custom', 'skill.options').split('|')

                if ticket_type == 'ECR' and 'SYS' in skills:
                    # Type help
                    stream |= Transformer('//select[@id="field-skill"]').after(tag.img(id_="type-help", src_="/htdocs/help-mini.jpg"))
                    stream |= Transformer('//img[@id="type-help"]').attr(
                        "style", "vertical-align:middle;").attr(
                        "title", "If several skills may be impacted, create first an ECR for the SYS skill").attr(
                        "class", "tooltip")

            elif ticket_type == 'MOM':

                # Date hint
                stream |= Transformer('//input[@id="field-duedate"]').after(tag.em('Format: %s' % get_date_format_hint(), style_='color:#888888;font-size:smaller;'))

                # Ticket properties CCB Milestone Tag help
                stream |= Transformer('//select[@id="field-milestonetag"]').attr(
                    "data-tooltip-content", "#milestonetag_help").attr(
                        "class", "tooltip")

                stream |= Transformer('//select[@id="field-milestonetag"]').after(
                    tag.span(tag.a('Explain',
                                   href_='%s/index.php?post/91' % dc_url),
                             style_='margin-left:10px;',
                             id_='explain-milestonetag'))

            elif ticket_type == 'DOC':

                branch_segregation_activated = True if self.env.config.get('artusplugin', 'branch_segregation_activated') == 'True' else False
                branch_segregation_first_branch = self.env.config.get('artusplugin', 'branch_segregation_first_branch', 'B1')
                add_script_data(req, g_branch_segregation_activated=branch_segregation_activated, g_branch_segregation_first_branch=branch_segregation_first_branch)

                if 'TICKET_ADMIN' in req.perm:
                    # The versionsuffix input field is left modifiable by admin
                    stream |= Transformer('//*[@id="field-versionsuffix"]').attr("readonly", None)
                    stream |= Transformer('//*[@id="field-versionsuffix"]').attr("style", "background-color:white")

                # Type help
                stream |= Transformer('//select[@id="field-configurationitem"]').after(tag.img(id_="type-help", src_="/htdocs/help-mini.jpg"))
                stream |= Transformer('//img[@id="type-help"]').attr(
                    "style", "vertical-align:middle;").attr(
                    "data-tooltip-content", "#configuration_item_help").attr(
                    "class", "tooltip")

                # A button is added in order to select the source url of the document
                sourceurl = req.args.get('sourceurl') or ''
                urltracbrowse = self.env.base_url + '/browser' + util.get_url(sourceurl)
                stream |= Transformer('//select[@id="field-configurationitem"]').after(
                    tag.input(value_="Browse",
                              name_='ci_select',
                              type_='button',
                              onclick_='location.href="%s"' % urltracbrowse,
                              title_='%s' % sourceurl,
                              class_=sourceurl and 'tooltip'))

                # The input field is replaced by a select field
                stream |= Transformer('//input[@id="field-sourcetype"]').replace(
                    tag.select(
                        [tag.option(DOC_UI.sourcetype_tip(self.env).keys()[1])],
                        id_="field-sourcetype",
                        name_="field_sourcetype",
                        title_=DOC_UI.sourcetype_tip(self.env)["select"],
                        class_="tooltip",
                        onchange_="on_sourcetype_change();"
                    )
                )

                # Link to the document properties panel
                stream |= Transformer('//select[@id="field-sourcetype"]').after(
                    tag.a(
                        "Change",
                        id="change_st",
                        title="Click here in order to change the source type for this document",
                        href="",
                        target="_blank",
                        style="margin-left:10px;",
                    )
                )

                stream |= Transformer('//input[@id="field-pdfsigned"]').after(
                    tag.a(
                        "Change",
                        id="change_ps",
                        title="Click here in order to change the PDF signing for this document",
                        href="",
                        target="_blank",
                        style="margin-left:10px;",
                    )
                )

                stream |= Transformer('//select[@id="field-controlcategory"]').after(
                    tag.a('Change',
                          id='change_cc',
                          title='Click here in order to change the control category for this document',
                          href='',
                          target='_blank',
                          style='margin-left:10px;'))
                
                stream |= Transformer('//select[@id="field-submittedfor"]').after(
                    tag.a('Change',
                          id='change_sf',
                          title='Click here in order to change the submission criteria for this document',
                          href='',
                          target='_blank',
                          style='margin-left:10px;'))

                stream |= Transformer('//input[@id="field-independence"]').after(
                    tag.a(
                        "Change",
                        id="change_ip",
                        title="Click here in order to change the independence for this document",
                        href="",
                        target="_blank",
                        style="margin-left:10px;",
                    )
                )

        if trac_id:

            add_script_data(req, g_ticket_owner=ticket['owner'])
            add_script_data(req, g_ticket_status=ticket['status'])
            add_script_data(req, g_ticket_resolution=ticket['resolution'])
            add_script_data(req, g_ticket_skill=ticket['skill'])
            add_script_data(req, g_ticket_momtype=ticket['momtype'])
            add_script_data(req, g_ticket_ecmtype=ticket['ecmtype'])
            add_script_data(req, g_trac_data_modified=False)

            ticket_edit = data['ticket_UI'].ticket_edit
            tickets_with_forms = set(('EFR', 'ECR', 'RF', 'PRF'))

            if 'MOM' in util.get_prop_values(self.env,
                                             'ticket_edit.office_suite').keys():
                tickets_with_forms.add('MOM')
                add_script_data(req, g_ticket_momform='Archived')
            else:
                add_script_data(req, g_ticket_momform='Other')

            if ticket['type'] in tickets_with_forms:
                tp_data = form.TicketForm.get_ticket_process_data(self.env, req.authname, ticket)
                tf = tp_data['ticket_form']
                if not util.exist_in_repo(self.env, tf.http_url):
                    tickets_with_forms.remove(ticket['type'])

            if ticket['type'] in tickets_with_forms:

                # A button is added in order to edit or view the ticket
                edit_mode, force_mode, checked = util.get_edit_or_view_mode(self.env, req, ticket, 'TICKET_FORCE_EDIT')

                stream |= Transformer('//input[@id="field-summary"]').after(tag.a(
                    'Not working ?',
                    id_='config',
                    title_="Click here if you're experiencing trouble accessing the %s form" % ticket_edit['office_suite'][ticket_type],
                    href="%s/index.php?post/76" % dc_url,
                    style_='margin-left:10px;'))

                if ticket_type == 'MOM' and edit_mode:
                    stream |= Transformer('//input[@id="field-summary"]').after(tag.input(
                        value_='Regenerate',
                        title_="Pre-fill the CCB or Review MOM or empty other types of MOM",
                        name_='regenerate',
                        id_='regenerate',
                        type_='button',
                        style_='margin-left:10px;',
                        onclick_='mom_regenerate("%s", "%s")' % (self.trac_env_name, trac_id)))

                if edit_mode is True:
                    label = 'Edit'
                else:
                    label = 'View'

                stream |= Transformer('//input[@id="field-summary"]').after(tag.input(
                    value_=label,
                    title_="%s the ticket with %s" % (label, ticket_edit['office_suite'][ticket_type]),
                    name_='ticket_edit',
                    id_='ticket_edit',
                    type_='button',
                    style_='margin-left:10px;'))

                if edit_mode is True:
                    href = tf.webdav_fileurl
                else:
                    href = util.get_trac_browser_url(self.env, tf.ticket_filename, 'HEAD')

                stream |= Transformer('//input[@name="ticket_edit"]').attr(
                    'onclick', 'location.href="%s"' % href)

                # A checkbox is added in order to force edit mode or lock (MOM)
                if force_mode is True:
                    if ticket_type == 'MOM':
                        title = "Lock to access edit mode"
                        onclick = 'mom_lock_unlock("%s")' % trac_id
                    else:
                        title = "Force the edit mode"
                        onclick = ('force_edit_mode("%s", "%s")' %
                                   (self.trac_env_name, trac_id))
                    kargs = {'type_': "checkbox",
                             'title_': title,
                             'name_': "force-edit-mode",
                             'id_': "force-edit-mode",
                             'style_': 'margin-left:5px;',
                             'onclick_': onclick
                             }
                    if checked:
                        kargs['checked_'] = 'checked'
                    stream |= Transformer('//input[@name="ticket_edit"]').after(tag.input(**kargs))

                    if ticket_type == 'MOM':
                        label = u"\u2192 Lock"
                    else:
                        label = u"\u2192 Edit"

                    stream |= Transformer('//input[@name="force-edit-mode"]').after(tag.label(
                        label,
                        for_="force-edit-mode",
                        title_=title))

                # Actions allowed or not
                if ticket_type == 'MOM':
                    if checked:
                        add_script_data(req, g_mom_lock=True)
                    else:
                        add_script_data(req, g_mom_lock=False)

                if ((ticket_type == 'EFR' and 'VERSION_TAG_DELETE' in req.perm) or
                    (ticket_type == 'ECR' and 'TICKET_ADMIN' in req.perm)):
                    # A button is added in order to select the baseline
                    urltracbrowse = self.env.base_url + '/browser' + (req.args.get('documenturl') or '/tags')
                    urltracbrowse = util.url_add_params(urltracbrowse, [('caller', 't%s' % trac_id)])
                    stream |= Transformer('//*[@for="field-document"]').replace(
                        tag.input(value_="Baseline tag:",
                                  name_='doc_CI_select',
                                  type_='button',
                                  onclick_='location.href="%s"' % urltracbrowse,
                                  title='%s' % urltracbrowse))

                if ticket_type == 'ECR':
                    # The requirements field label is completed with a link to report 'List of requirements impacted by ECRs'
                    report_title1 = 'List of requirements impacted by ECRs'
                    report_id1 = self._reports[report_title1]
                    report_title2 = 'ECRs impacting same requirements'
                    report_id2 = self._reports[report_title2]
                    stream |= Transformer('//label[@for="field-requirements"]').after(
                        tag.span("/",
                                 tag.a("#%s" % report_id1,
                                       href_="%s/report/%s" % (req.base_url, report_id1),
                                       title_=report_title1,
                                       class_="tooltip"))).after(
                        tag.span("Check reports ",
                                 tag.a("#%s" % report_id2,
                                       href_="%s/report/%s" % (req.base_url, report_id2),
                                       title_=report_title2,
                                       class_="tooltip"))).after(tag.br()).after(tag.br())

                    # Requirements
                    stream |= Transformer('//td[@headers="h_requirements"]/text()').apply(self.to_non_breaking_hyphen)

            if ticket_type in ('RF', 'PRF'):
                add_script_data(req, g_p_rf_parent_ticket=ticket['parent'])
            elif ticket_type == 'MOM':
                # QMS link
                if ticket['qmsref']:
                    stream |= Transformer('//td[@headers="h_qmsref"]/text()'
                                          ).replace(tag.a(ticket['qmsref'],
                                                          id_='qmslink',
                                                          title_='Enter the Quality Management System for viewing the Report',
                                                          href=self.env.config.get('artusplugin', 'QMS_url') % ticket['qmsref']))

                # Tag url link
                stream |= Transformer('//td[@headers="h_milestonetag"]/a/text()'
                                      ).apply(self.to_non_breaking_hyphen)
                href = Href(self.env.base_url)
                if ticket['milestonetag']:
                    stream |= Transformer('//td[@headers="h_milestonetag"]/a'
                                          ).attr("href",
                                                 href.admin('tags_mgmt',
                                                            'milestone_tags',
                                                            ticket['milestonetag']))

            elif ticket_type == 'RISK':
                # Ticket box impact rating field
                if ticket['rating']:
                    stream |= Transformer('//td[@headers="h_rating"]/text()').replace(tag.input(value_=ticket['rating'], id_='h_rating_input', type_='text'))
                    stream |= Transformer('//input[@id="h_rating_input"]').attr(
                        "readonly", "true").attr(
                        "style", "font-weight:bold;font-size:80%%;text-align:center;background-color:%s;color:%s" % (
                            self._rating_rendering['bgcolor'][ticket['rating']],
                            self._rating_rendering['color'][ticket['rating']])).attr(
                        "size", "4")

                # Ticket properties Probability help
                stream |= Transformer('//select[@id="field-probability"]').after(tag.img(id_="properties-probability-help", src_="/htdocs/help-mini.jpg"))

                # Help pop-up is attached to probability-help image in ticket properties
                stream |= Transformer('//img[@id="properties-probability-help"]').attr(
                    "style", "margin: 0em 0.5em;vertical-align:middle;").attr(
                    "data-tooltip-content", "#rating_help").attr(
                    "class", "tooltip")

                # Ticket properties Impact help
                stream |= Transformer('//select[@id="field-impact"]').after(tag.img(id_="properties-impact-help", src_="/htdocs/help-mini.jpg"))

                # Help pop-up is attached to impact-help image in ticket properties
                stream |= Transformer('//img[@id="properties-impact-help"]').attr(
                    "style", "margin: 0em 0.5em;vertical-align:middle;").attr(
                    "data-tooltip-content", "#rating_help").attr(
                    "class", "tooltip")

                # Ticket properties Rating help
                stream |= Transformer('//input[@id="field-rating"]').after(tag.img(id_="properties-rating-help", src_="/htdocs/help-mini.jpg"))

                # Help pop-up is attached to rating-help image in ticket properties
                stream |= Transformer('//img[@id="properties-rating-help"]').attr(
                    "style", "margin: 0em 0.5em;vertical-align:middle;").attr(
                    "data-tooltip-content", "#rating_help").attr(
                    "class", "tooltip")

                # Description help
                stream |= Transformer('//th/label[@for="field-description"]').after(tag.img(id_="description-help", src_="/htdocs/help-mini.jpg"))

                # Help pop-up is attached to description-help image in ticket properties
                stream |= Transformer('//img[@id="description-help"]').attr(
                    "style", "margin: 0em 0.5em;vertical-align:middle;").attr(
                    "data-tooltip-content", "#description_help").attr(
                    "class", "tooltip")

            elif ticket_type == 'AI':
                # Date hint
                stream |= Transformer('//input[@id="field-duedate"]').after(tag.em('Format: %s' % get_date_format_hint(), style_='color:#888888;font-size:smaller;'))

                # Description help
                stream |= Transformer('//th/label[@for="field-description"]').after(tag.img(id_="description-help", src_="/htdocs/help-mini.jpg"))

                # Help pop-up is attached to description-help image in ticket properties
                stream |= Transformer('//img[@id="description-help"]').attr(
                    "style", "margin: 0em 0.5em;vertical-align:middle;").attr(
                    "title", "Describe the Action Item").attr(
                    "class", "tooltip")

                # Explicit parent ticket type
                match = re.search('\A#(\d+)\Z', ticket['parent'])
                if match:
                    parent_tkt = Ticket(self.env, match.group(1))
                    if parent_tkt['type'] == 'MOM':
                        label = 'Parent MOM'
                    elif parent_tkt['type'] == 'RISK':
                        label = 'Parent RISK'
                    else:
                        label = 'Parent Ticket'

                    def set_ticket_type(stream):

                        for mark, (kind, data, pos) in stream:
                            if mark and kind is TEXT:
                                yield mark, (kind, data.replace('Parent Ticket', label), pos)
                            else:
                                yield mark, (kind, data, pos)

                    # Ticket box
                    stream |= Transformer('//th[@id="h_parent"]/text()').apply(set_ticket_type)

                    # Properties
                    stream |= Transformer('//label[@for="field-parent"]/text()').apply(set_ticket_type)

            elif ticket_type == 'MEMO' or (ticket_type == 'ECM' and Ticket_UI.get_UI(ticket).legacy):
                # Description help
                stream |= Transformer('//th/label[@for="field-description"]').after(tag.img(id_="description-help", src_="/htdocs/help-mini.jpg"))

                # Help pop-up is attached to description-help image in ticket properties
                stream |= Transformer('//img[@id="description-help"]').attr(
                    "style", "margin: 0em 0.5em;vertical-align:middle;").attr(
                    "title", "Describe briefly the %s content" % ticket_type).attr(
                    "class", "tooltip")

                if 'TICKET_ADMIN' in req.perm:
                    # The document input field is left modifiable by admin
                    stream |= Transformer('//*[@id="field-document"]').attr("readonly", None)
                    stream |= Transformer('//*[@id="field-document"]').attr("style", "background-color:white")

            elif (ticket_type == 'ECM' and not Ticket_UI.get_UI(ticket).legacy) or ticket_type == 'FEE' or ticket_type == 'DOC':

                if ticket['status'] == '01-assigned_for_edition':
                    # A button is added in order to browse the source url (see doc_sourceurl in artus.js)
                    sourceurl = req.args.get('sourceurl') or ticket['sourceurl']
                    urltracbrowse = '%s/browser%s' % (self.env.base_url, sourceurl)
                    urltracbrowse = util.url_add_params(urltracbrowse,
                                                        [('caller', 't%s' % trac_id)])
                    add_script_data(req, g_doc_urltracbrowse=urltracbrowse, g_doc_sourceurl=sourceurl)

                    if util.get_url(sourceurl) != util.get_url(ticket['sourceurl']):
                        # The url has changed
                        add_script_data(req, g_doc_lock_unlock_disabled='disabled')
                    else:
                        template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
                        with template_cls(self.env,
                                          self.trac_env_name,
                                          req.authname,
                                          ticket) as doc:
                            # A button is added in order to view or edit
                            # the document source file (see doc_sourcefile() in artus.js)
                            selected_src = ""
                            if ticket['sourcefile'] and ticket['sourcefile'] != 'N/A':
                                selected_src = ticket['sourcefile']
                                suffixes = util.get_prop_values(self.env, 'source_files_suffix')
                                suffix = selected_src.split('.')[-1]
                                editable = suffix in suffixes
                            else:
                                editable = False
                                add_script_data(req, g_doc_lock_unlock_disabled='disabled')

                            # Source status
                            src_wc_status = doc.status(ticket['sourcefile'], 'wc-status')
                            src_repos_status = doc.status(ticket['sourcefile'], 'repos-status')

                            if (src_wc_status['lock_agent'] == 'trac' and
                                src_wc_status['lock_client'] == req.authname and
                                src_repos_status['lock_agent'] == 'trac' and
                                src_repos_status['lock_client'] == req.authname):
                                edit_mode = True
                            else:
                                edit_mode = False

                            # Two radio buttons are added in order to lock/unlock the
                            # source & pdf files (see doc_sourcefile() in artus.js)
                            if editable:
                                if edit_mode:
                                    add_script_data(req, g_doc_lock=True)
                                else:
                                    add_script_data(req, g_doc_lock=False)
                                if (ticket['owner'] != req.authname):
                                    add_script_data(req, g_doc_lock_unlock_disabled='disabled')

                            if edit_mode:
                                label = 'Edit'
                            else:
                                label = 'View'

                            add_script_data(req, g_doc_sourcefile_button_label=label)

                            if selected_src:
                                webdav_edit_url = doc.get_webdav_url(selected_src, 'edit')
                                webdav_view_url = doc.get_webdav_url(selected_src, 'view')
                                add_script_data(req, g_doc_sourcefile_button_webdav_edit_url=webdav_edit_url)
                                add_script_data(req, g_doc_sourcefile_button_webdav_view_url=webdav_view_url)
                                if ticket_type == 'DOC':
                                    add_script_data(req, g_doc_sourcefile_status='Draft')
                                add_script_data(req, g_doc_sourcefile_charts='Image')

                            # A button is added in order to view
                            # the document pdf file
                            if not ticket['pdffile']:
                                pdf_files = [(filename, filename.rsplit('.', 1)[0] ==
                                              ticket['configurationitem'])
                                             for filename in doc.get_pdffile_list()]

                            selected_pdf = ""
                            if not ticket['pdffile']:
                                if pdf_files:
                                    selected = [f[0] for f in pdf_files if f[1]]
                                    if selected:
                                        selected_pdf = selected[0]
                                    else:
                                        selected_pdf = pdf_files[0]
                            elif ticket['pdffile'] != 'N/A':
                                selected_pdf = ticket['pdffile']

                            if selected_pdf:
                                href = doc.get_webdav_url(selected_pdf, 'view')
                                add_script_data(req, g_doc_pdffile_button_href=href)
                            add_script_data(req, g_doc_pdffile_attachments='AttachmentsNotIncluded')
                            add_script_data(req, g_doc_pdffile_markups='WithoutMarkups')

                            if ticket_type == 'DOC':
                                source_types = util.get_prop_values(self.env, "source_types")
                                sourcetype = ticket['sourcetype']
                                automation = source_types[sourcetype].split('||')[5].strip()
                                add_script_data(req, g_doc_automation=automation)
                            else:
                                add_script_data(req, g_doc_automation='full')

                            # Check existence of attachments for setting default value
                            # of choice re inclusion into the generated PDF
                            if (ticket_type == 'DOC' and
                                ticket['sourcefile'] and ticket['sourcefile'] != 'N/A' and
                                ticket['pdffile'] and ticket['pdffile'] != 'N/A' and
                                ticket['sourcefile'].split('.')[-1] == 'docm'):
                                attachments = [attachment for attachment in
                                               Attachment.select(self.env, 'ticket', ticket.id)]
                                if attachments:
                                    add_script_data(req, g_doc_exist_attachments=True)
                                else:
                                    add_script_data(req, g_doc_exist_attachments=False)


                if 'TICKET_ADMIN' in req.perm:
                    # The document input field is left modifiable by admin
                    add_script_data(req, g_doc_tag_admin='admin')

                # Ticket properties CCB MOM help
                stream |= Transformer('//input[@id="field-parent"]').attr(
                    "data-tooltip-content", "#ccb_mom_help").attr(
                        "class", "tooltip")

        if ticket_type == 'EFR':
            # EfrHowTo wiki page links
            stream |= Transformer('//label[@for="field-company"]/text()').wrap(
                tag.a(id_='company', href_='%s/index.php?post/107#Company' % dc_url))
            stream |= Transformer('//label[@for="field-severity"]/text()').wrap(
                tag.a(id_='severity', href_='%s/index.php?post/107#Severity' % dc_url))
            stream |= Transformer('//label[@for="field-keywords"]/text()').wrap(
                tag.a(id_='keywords', href_='%s/index.php?post/107#Keywords' % dc_url))
            stream |= Transformer('//label[@for="field-phase"]/text()').wrap(
                tag.a(id_='phase', href_='%s/index.php?post/107#Phase' % dc_url))
            stream |= Transformer('//label[@for="field-document"]/text()').wrap(
                tag.a(id_='baseline', href_='%s/index.php?post/107#Baseline' % dc_url))
            stream |= Transformer('//select[@id="resolve_resolve_resolution"]').after(
                tag.span(tag.a('Explain',
                               href_='%s/index.php?post/107#Resolution' % dc_url),
                         style_='margin-left:10px;'))
            stream |= Transformer('//select[@id="change_resolution_resolve_resolution"]').after(
                tag.span(tag.a('Explain',
                               href_='%s/index.php?post/107#Resolution' % dc_url),
                         style_='margin-left:10px;'))

        if ticket_type == 'ECR':
            # EcrHowTo wiki page links
            stream |= Transformer('//label[@for="field-company"]/text()').wrap(
                tag.a(id_='company', href_='%s/index.php?post/108#Company' % dc_url))
            stream |= Transformer('//label[@for="field-keywords"]/text()').wrap(
                tag.a(id_='keywords', href_='%s/index.php?post/108#Keywords' % dc_url))
            stream |= Transformer('//label[@for="field-ecrtype"]/text()').wrap(
                tag.a(id_='ecrtype', href_='%s/index.php?post/108#EcrType' % dc_url))
            stream |= Transformer('//label[@for="field-milestone"]/text()').wrap(
                tag.a(id_='milestone', href_='%s/index.php?post/108#Milestone' % dc_url))
            stream |= Transformer('//label[@for="field-document"]/text()').wrap(
                tag.a(id_='baseline', href_='%s/index.php?post/108#Baseline' % dc_url))
            stream |= Transformer('//label[@for="field-blocking"]/text()').wrap(
                tag.a(id_='blocking', href_='%s/index.php?post/108#Blocking' % dc_url))
            stream |= Transformer('//label[@for="field-blockedby"]/text()').wrap(
                tag.a(id_='blockedby', href_='%s/index.php?post/108#BlockedBy' % dc_url))
            stream |= Transformer('//select[@id="resolve_resolve_resolution"]').after(
                tag.span(tag.a('Explain',
                               href_='%s/index.php?post/108#Resolution' % dc_url),
                         style_='margin-left:10px;'))
            stream |= Transformer('//select[@id="change_resolution_resolve_resolution"]').after(
                tag.span(tag.a('Explain',
                               href_='%s/index.php?post/108#Resolution' % dc_url),
                         style_='margin-left:10px;'))

        if ticket_type == 'DOC':
            # Ticket properties Parent Ticket(s) help
            stream |= Transformer('//input[@id="field-blocking"]').attr(
                "data-tooltip-content", "#parent_ticket_help").attr(
                "class", "tooltip")

        return stream

    def _filter_ticket_delete_stream(self, req, method, filename, stream, data):

        # get ticket type
        trac_id = req.args.get('id')
        ticket = Ticket(self.env, trac_id)
        ticket_type = ticket['type']

        # Hide the 'Document url' field
        stream |= Transformer('//*[@id="h_documenturl"]').remove()
        stream |= Transformer('//*[@headers="h_documenturl"]').remove()

        if ticket_type == 'ECR':

                # The blocking field label is displayed according to ECR Type
                ecrtype = ticket['ecrtype']
                if ecrtype == 'Evolution':
                    stream |= Transformer('//th[@id="h_blocking"]/text()').replace("Parent ECR(s):")
                else:
                    stream |= Transformer('//th[@id="h_blocking"]/text()').replace("Parent EFR(s):")

                # The blockedby field label depends on DOC type being available
                blockedby_label = Ticket_UI.field_label(ticket, 'blockedby')
                stream |= Transformer('//th[@id="h_blockedby"]/text()').replace(blockedby_label)

                if 'artusplugin' in data:
                    for field, value in data['artusplugin']['field_values'].iteritems():
                        stream |= Transformer('//td[@headers="h_%s"]/text()' % field).replace(value)

                # Requirements
                stream |= Transformer('//td[@headers="h_requirements"]/text()').apply(self.to_non_breaking_hyphen)

        # QMS link
        elif ticket_type == 'MOM':
            if ticket['qmsref']:
                stream |= Transformer('//td[@headers="h_qmsref"]/text()'
                                      ).replace(tag.a(ticket['qmsref'],
                                                      id_='qmslink',
                                                      title_='Enter the Quality Management System for viewing the Report',
                                                      href=self.env.config.get('artusplugin', 'QMS_url') % ticket['qmsref']))

            # Tag url link
            stream |= Transformer('//td[@headers="h_milestonetag"]/a/text()'
                                  ).apply(self.to_non_breaking_hyphen)
            try:
                tag_url = model.Tag(self.env, ticket['milestonetag']).tag_url
                stream |= Transformer('//td[@headers="h_milestonetag"]/a'
                                      ).attr("href", "%s/browser%s" % (req.base_path, tag_url))
            except Exception:
                pass

        elif ticket_type == 'RISK':
            # Ticket box impact rating field
            if ticket['rating']:
                stream |= Transformer('//td[@headers="h_rating"]/text()').replace(tag.input(value_=ticket['rating'], id_='h_rating_input', type_='text'))
                stream |= Transformer('//input[@id="h_rating_input"]').attr(
                    "readonly", "true").attr(
                    "style", "font-weight:bold;font-size:80%%;text-align:center;background-color:%s;color:%s" % (
                        self._rating_rendering['bgcolor'][ticket['rating']],
                        self._rating_rendering['color'][ticket['rating']])).attr(
                    "size", "4")

        return stream

    def _filter_browser_stream(self, req, method, filename, stream, data):

        href = data['path_links'][-1]['href']

        # Clean Repository URL:
        # eg: removes 'caller' from query string
        #     add revision
        repo_url = util.repo_url(util.get_url(href))
        href_val = util.get_repo_url(self.env, repo_url)
        revision = util.get_revision(href)
        if revision:
            href_val = '%s?r=%s' % (href_val, revision)
        stream |= Transformer('//div[@id="ctxtnav"]/ul/li[@class="last"]/a').attr(
            'href', href_val)

        # Create button for getting back to attachment selection form
        if 'attachment_tid' in req.args:
            attachment_tid = req.args.get('attachment_tid')
            url_attach_file = '%s/attachment/ticket/%s/' % (self.env.base_url,
                                                            attachment_tid)
            stream |= Transformer('//form[@name="select_file"]').attr(
                'action',
                '%(url_attach_file)s' % {'url_attach_file': url_attach_file})

        if not util.node_is_dir(
                self.env,
                util.repo_url(href)):
            return stream

        def more_buttons_notification_display(stream):
            stream |= Transformer('//div[@id="anydiff"]/form/div[@class="buttons"]').after(
                tag.div(tag.strong('Note: '),
                    'Remove the specified revision to gain access to more buttons',
                    id_='help', style_='text-align: left'))
            return stream

        def branch_segregation_notification_display(stream, button_label):
            stream |= Transformer('//div[@id="anydiff"]/form/div[@class="buttons"]').after(
                tag.div(tag.p(tag.strong('Note: '),
                    'Choose another branch with an id greater or equal to the <branch_segregation_first_branch> parameter value '
                    'to gain access to the <%s> button' % button_label,
                    id_='help', style_='text-align: left')))
            return stream

        if ('/tags/versions/' in req.path_info or
            '/tags/milestones/' in req.path_info):

            # RF / PRF / EFR / ECR / MEMO buttons display management
            tagname = data['path_links'][-1]['name']

            if model.NamingRule.is_tag_dir(self.env, tagname, self.program_name,
                                           data['path_links'][-2]['name'],
                                           data['path_links'][-3]['name']):
                path = util.get_path(req.path_info)

                if path and path[1] == 'versions' and 'TICKET_CREATE' in req.perm:

                    def back_to_efr(stream):
                        # 'Back to EFR...'
                        if revision == '':
                            stream |= Transformer('//div[@id="anydiff"]/form'
                                                  '/div/input[@type="submit"]').after(
                                tag.div(tag.input(value_="Back to EFR ...",
                                                  name_='ticket_modification',
                                                  title_=('Back to ticket %s' %
                                                          trac_id),
                                                  type_='button',
                                                  class_='buttons'),
                                        style_='margin-left:10px;'))
                            url = Href(self.env.base_url)
                            url_ticket_modification = url.ticket(trac_id,
                                                                 document=tagname,
                                                                 documenturl=docurl)
                            stream |= Transformer('//input[@name='
                                                  '"ticket_modification"]').attr(
                                'onclick', 'location.href="%s"' % url_ticket_modification)
                        else:
                            # Access to more buttons
                            stream = more_buttons_notification_display(stream)

                        return stream

                    def back_to_ecr(stream):
                        # 'Back to ECR...'
                        if revision == '':
                            stream |= Transformer('//div[@id="anydiff"]/form'
                                                  '/div/input[@type="submit"]').after(
                                tag.div(tag.input(value_="Back to ECR ...",
                                                  name_='ticket_modification',
                                                  title_=('Back to ticket %s' %
                                                          trac_id),
                                                  type_='button',
                                                  class_='buttons'),
                                        style_='margin-left:10px;'))
                            url = Href(self.env.base_url)
                            url_ticket_modification = url.ticket(trac_id,
                                                                 document=tagname,
                                                                 documenturl=docurl)
                            stream |= Transformer('//input[@name='
                                                  '"ticket_modification"]').attr(
                                'onclick', 'location.href="%s"' % url_ticket_modification)
                        else:
                            # Access to more buttons
                            stream = more_buttons_notification_display(stream)

                        return stream

                    def create_efr(stream):
                        # 'Create EFR...'
                        if revision == '':
                            stream |= Transformer('//div[@id="anydiff"]/form/div/input[@type="submit"]').after(
                                tag.div(tag.input(value_="Create EFR...",
                                                  name_='EFR_creation',
                                                  title_="The EFR will be created on the selected tag",
                                                  type_='button',
                                                  class_='buttons'),
                                        style_='margin-left:10px;'))
                            url = Href(self.env.base_url)
                            url_efr_creation = url.newticket(type='EFR',
                                                             document=tagname,
                                                             documenturl=docurl,
                                                             skill=skill)
                            stream |= Transformer('//input[@name="EFR_creation"]').attr(
                                'onclick', 'location.href="%s"' %
                                url_efr_creation)
                        else:
                            # Access to more buttons
                            stream = more_buttons_notification_display(stream)

                        return stream

                    def create_ecr(stream):
                        if revision == '':
                            # 'Create ECR...'
                            stream |= Transformer('//div[@id="anydiff"]/form/div/input[@type="submit"]').after(
                                tag.div(tag.input(value_="Create ECR...",
                                                  name_='ECR_creation',
                                                  title_="The ECR will be created on the selected tag",
                                                  type_='button',
                                                  class_='buttons'),
                                        style_='margin-left:10px;'))
                            url = Href(self.env.base_url)
                            url_ecr_creation = url.newticket(type='ECR',
                                                             document=tagname,
                                                             documenturl=docurl,
                                                             skill=skill)
                            stream |= Transformer('//input[@name="ECR_creation"]').attr(
                                'onclick', 'location.href="%s"' %
                                url_ecr_creation)
                        else:
                            # Access to more buttons
                            stream = more_buttons_notification_display(stream)

                        return stream

                    def create_rf(stream):
                        if revision == '':
                            # 'Create RF...'
                            if data['path_links'][-2]['name'] != 'Released':
                                stream |= Transformer('//div[@id="anydiff"]/form/div/input[@type="submit"]').after(
                                    tag.div(tag.input(value_="Create RF...",
                                                      name_='RF_creation',
                                                      title_="The RF will be created on the selected tag",
                                                      type_='button',
                                                      class_='buttons'),
                                            style_='margin-left:10px;'))
                                url = Href(self.env.base_url)
                                url_rf_creation = url.newticket(type='RF',
                                                                document=tagname,
                                                                documenturl=docurl)
                                stream |= Transformer('//input[@name="RF_creation"]').attr(
                                    'onclick', 'location.href="%s"' %
                                    url_rf_creation)
                        else:
                            # Access to more buttons
                            stream = more_buttons_notification_display(stream)

                        return stream

                    def create_prf(stream):
                        # 'Create PRF...'
                        v = model.Tag(self.env, name=tagname)
                        if (tagname.startswith('ECM_%s_' % self.program_name) and
                            re.search(model.NamingRule.get_ecm_pattern(self.program_name), tagname, re.UNICODE)):
                            # ECM
                            if v.status_index is not None:
                                show_button = True
                                tktid = util.get_ecm_tktid(self.env, v.tagged_item)
                            else:
                                show_button = False
                                tktid = None
                        elif (tagname.startswith('FEE_%s_' % self.program_name) and
                            re.search(model.NamingRule.get_fee_pattern(self.program_name), tagname, re.UNICODE)):
                            # FEE
                            if v.status_index is not None:
                                show_button = True
                                tktid = util.get_fee_tktid(self.env, v.tagged_item)
                            else:
                                show_button = False
                                tktid = None
                        else:
                            # DOC
                            if v.status != 'Released':
                                show_button = True
                                skill = (util.get_doc_skill(
                                            self.env,
                                            v.tagged_item,
                                            self.program_name)
                                            if 'DOC' in self._ticket_types
                                            else None)
                                tktid = (util.get_doc_tktid(
                                            self.env,
                                            v.tagged_item)
                                            if skill
                                            else None)
                            else:
                                show_button = False
                                tktid = None

                        if show_button:
                            if not tktid:

                                if revision == '':
                                    stream |= Transformer('//div[@id="anydiff"]/form/div/input[@type="submit"]').after(
                                        tag.div(tag.input(value_="Create PRF...",
                                                        name_='PRF_creation',
                                                        title_="The PRF will be created on the selected tag",
                                                        type_='button',
                                                        class_='buttons'),
                                                style_='margin-left:10px;'))
                                    url = Href(self.env.base_url)
                                    url_prf_creation = url.newticket(type='PRF',
                                                                    document=tagname,
                                                                    documenturl=docurl)
                                    stream |= Transformer('//input[@name="PRF_creation"]').attr(
                                        'onclick', 'location.href="%s"' %
                                        url_prf_creation)
                                else:
                                    # Access to more buttons
                                    stream = more_buttons_notification_display(stream)

                            else:
                                tkt = Ticket(self.env, tktid)
                                stream |= Transformer('//div[@id="anydiff"]/form/div[@class="buttons"]').after(
                                    tag.div(tag.strong('Note: '),
                                            'To create a PRF on this version tag '
                                            'do it through the %s ticket ' % tkt['type'],
                                            tag.a('#%s' % tktid,
                                                href=req.href.ticket(tktid),
                                                title_=tkt['summary']),
                                            id_='help', style_='text-align: left'))

                        return stream

                    def create_memo(stream):
                        if revision == '':
                            # 'Create MEMO...'
                            stream |= Transformer('//div[@id="anydiff"]/form/div/input[@type="submit"]').after(
                                tag.div(tag.input(value_="Create MEMO...",
                                                  name_='MEMO_creation',
                                                  title_="The MEMO will be created on the selected tag",
                                                  type_='button',
                                                  class_='buttons'),
                                        style_='margin-left:10px;'))
                            url = Href(self.env.base_url)
                            url_memo_creation = url.newticket(type='MEMO',
                                                              document=tagname,
                                                              documenturl=docurl,
                                                              skill=skill)
                            stream |= Transformer('//input[@name="MEMO_creation"]').attr(
                                'onclick', 'location.href="%s"' %
                                url_memo_creation)
                        else:
                            # Access to more buttons
                            stream = more_buttons_notification_display(stream)

                        return stream

                    docurl = util.get_url(util.repo_url(href))
                    skill = util.get_skill(self.env, tagname, self.program_name)
                    if skill == 'EXT':
                        # Try and get real skill from repository path
                        skills = util.get_prop_values(self.env, 'skill_dirs')
                        regexp = '(%s)' % '|'.join(skills.values())
                        match = re.search(regexp, docurl)
                        if match:
                            reversed_skills = dict(zip(skills.values(), skills.keys()))
                            skill = reversed_skills[match.group(1)]

                    if 'caller' in req.args and isinstance(req.args.get('caller'), basestring):
                        caller = req.args.get('caller')
                        regexp = r"\At(\d+)\Z"
                        match = re.search(regexp, caller)
                        if match:
                            # Ticket created
                            trac_id = match.group(1)
                            try:
                                ticket = Ticket(self.env, trac_id)
                                if req.authname == ticket['owner']:
                                    if ticket['type'] == 'EFR' and 'VERSION_TAG_DELETE' in req.perm:
                                        stream = back_to_efr(stream)
                                    elif (ticket['type'] == 'ECR' and 'TICKET_ADMIN' in req.perm and
                                          model.NamingRule.get_status_from_tag(self.env, tagname) == 'Released'):
                                        stream = back_to_ecr(stream)
                            except Exception:
                                pass
                        else:
                            regexp = r"\At(EFR|ECR|RF|PRF)\Z"
                            match = re.search(regexp, caller)
                            if match:
                                # Ticket in creation
                                ticket_type = match.group(1)
                                if ticket_type == 'EFR':
                                    stream = create_efr(stream)
                                elif ticket_type == 'ECR':
                                    stream = create_ecr(stream)
                                elif ticket_type == 'RF':
                                    stream = create_rf(stream)
                                elif ticket_type == 'PRF':
                                    stream = create_prf(stream)
                    else:

                        if u'EFR' in self._ticket_types:
                            stream = create_efr(stream)

                        if (u'ECR' in self._ticket_types and 'VERSION_TAG_DELETE' in req.perm and
                            model.NamingRule.get_status_from_tag(self.env, tagname) == 'Released'):
                            stream = create_ecr(stream)

                        if u'RF' in self._ticket_types:
                            stream = create_rf(stream)

                        if u'PRF' in self._ticket_types:
                            stream = create_prf(stream)

                        if u'MEMO' in self._ticket_types:
                            stream = create_memo(stream)

        elif '/trunk/' in req.path_info or '/branches/' in req.path_info:

            # DOC button display management
            path = util.get_path(req.path_info)

            if path and path[0] in ('trunk', 'branches'):
                dir_name = data['path_links'][-1]['name']

                if ('DOC' in self._ticket_types and
                    model.NamingRule.is_ci_name(self.env, dir_name, self.program_name)):

                    regular_expression = (r"/tracs/%s/browser(/(?:\w+/)?"
                                          r"%s[^?]*)(?:\?(.+))?" % (self.trac_env_name,
                                                                    path[0]))
                    sourceurl = util.url_from_browse(self.env,
                                                     util.unicode_unquote_plus(href),
                                                     regular_expression)
                    display_button = False
                    skill = util.get_skill(self.env,
                                           dir_name,
                                           self.program_name)
                    DOC_skills = self.env.config.get('artusplugin',
                                                     'DOC_skills', '').split('|')
                    db = self.env.get_db_cnx()
                    cursor = db.cursor()
                    cursor.execute("SELECT DISTINCT tracked_item, component FROM tag "
                                   "WHERE tracked_item='%s'" % dir_name)
                    row = cursor.fetchone()
                    if row:
                        component = row[1]
                        if not component:
                            if skill in DOC_skills:
                                display_button = True
                    else:
                        if skill in DOC_skills:
                            display_button = True

                    if ('MILESTONE_CREATE' in req.perm and
                        display_button and 'caller' not in req.args):

                        branch_segregation_activated = True if self.env.config.get('artusplugin', 'branch_segregation_activated') == 'True' else False
                        if branch_segregation_activated:
                            branch_segregation_first_branch = self.env.config.get('artusplugin', 'branch_segregation_first_branch', 'B1')

                        if not branch_segregation_activated or path[0] == 'trunk' or int(path[1][1:]) >= int(branch_segregation_first_branch[1:]):
                            if revision == '':
                                # 'Create DOC...'
                                stream |= Transformer('//div[@id="anydiff"]/form'
                                                      '/div/input[@type="submit"]').after(
                                    tag.div(tag.input(value_="Create DOC...",
                                                      name_='DOC_creation',
                                                      title_=("The DOC will be created "
                                                              "on the selected configuration item"),
                                                      type_='button',
                                                      class_='buttons'),
                                            style_='margin-left:10px;'))
                                configurationitem=dir_name
                                source_types = util.get_prop_values(self.env, "source_types")
                                for st in source_types:
                                    regexp = source_types[st].split('||')[4].strip()
                                    if regexp:
                                        # pcre supports lookahead conditional (required for ATP), re doesn't
                                        match = pcre.search(regexp, configurationitem)
                                        if match:
                                            sourcetype = st
                                            break
                                else:
                                    sourcetype = None
                                url = Href(self.env.base_url)
                                url_doc_creation = url.newticket(type='DOC',
                                                                 skill=skill,
                                                                 configurationitem=configurationitem,
                                                                 sourceurl=sourceurl,
                                                                 sourcetype=sourcetype)
                                stream |= Transformer('//input[@name='
                                                      '"DOC_creation"]').attr(
                                    'onclick', ('location.href="%s"' % url_doc_creation))
                            else:
                                # Access to more buttons
                                stream = more_buttons_notification_display(stream)
                        else:
                            # Access to 'Create DOC' button
                            stream = branch_segregation_notification_display(stream, 'Create DOC')

                    if ('TICKET_MODIFY' in req.perm and
                        display_button and
                        'caller' in req.args and
                        isinstance(req.args.get('caller'), basestring)):
                        caller = req.args.get('caller')
                        regexp = r"\At(\d+)\Z"
                        match = re.search(regexp, caller)
                        if match:
                            trac_id = match.group(1)
                            try:
                                ticket = Ticket(self.env, trac_id)
                                ci_name = ticket['configurationitem']

                                if dir_name == ci_name and req.authname == ticket['owner']:

                                    if revision == '':
                                        # 'Back to DOC...'
                                        stream |= Transformer('//div[@id="anydiff"]/form'
                                                              '/div/input[@type="submit"]').after(
                                            tag.div(tag.input(value_="Back to DOC ...",
                                                              name_='ticket_modification',
                                                              title_=('Back to ticket %s' %
                                                                      trac_id),
                                                              type_='button',
                                                              class_='buttons'),
                                                    style_='margin-left:10px;'))
                                        url = Href(self.env.base_url)
                                        url_ticket_modification = url.ticket(trac_id,
                                                                             sourceurl=sourceurl)
                                        stream |= Transformer('//input[@name='
                                                              '"ticket_modification"]').attr(
                                            'onclick', 'location.href="%s"' % url_ticket_modification)
                                    else:
                                        # Access to more buttons
                                        stream = more_buttons_notification_display(stream)

                            except Exception:
                                pass

        return stream

    def _filter_attachment_stream(self, req, method, filename, stream, data):

        # WIKI
        dc_url = self.env.config.get('artusplugin', 'dc_url')

        # A button 'View/Edit' is to be added as is done for tickets
        trac_id = data['attachment'].parent_id
        ticket = Ticket(self.env, trac_id)
        ticket_type = ticket['type']
        attachment_edit = Ticket_UI.get_UI(ticket)(self, ticket).attachment_edit
        attachment_filename = data['attachment'].filename
        suffix = data['attachment'].filename.rsplit('.', 1)[-1]
        supported_suffix = suffix in attachment_edit['protocol'].keys()
        edit_mode, force_mode, checked = util.get_edit_or_view_mode(
            self.env,
            req,
            ticket, 'ATTACHMENT_FORCE_EDIT')

        if (supported_suffix and
            edit_mode is True):
            label = 'Edit'
            if attachment_edit['office_suite'][suffix] == 'MS Office':
                clickonce_app_url = self.env.config.get('artusplugin', 'clickonce_app_url_MS_Office')
            else:
                clickonce_app_url = self.env.config.get('artusplugin', 'clickonce_app_url_OpenOffice')
            tickets_with_forms = set(('EFR', 'ECR', 'RF', 'PRF'))

            if 'MOM' in util.get_prop_values(self.env,
                                             'ticket_edit.office_suite').keys():
                tickets_with_forms.add('MOM')

            if ticket['type'] in tickets_with_forms:
                tp_data = form.TicketForm.get_ticket_process_data(self.env, req.authname, ticket)
                tf = tp_data['ticket_form']
                if not util.exist_in_repo(self.env, tf.http_url):
                    tickets_with_forms.remove(ticket['type'])

            if ticket['type'] in tickets_with_forms:
                # Edition from the working copy
                # (see cache_path from webdav.conf)
                webdav_protocol = attachment_edit['protocol'][suffix]
                if webdav_protocol == 'http':
                    webdav_protocol = req.scheme
                urlwc = '%s://%s/tickets/%s/%s/%s' % (
                    webdav_protocol,
                    util.get_hostname(self.env),
                    self.trac_env_name,
                    req.authname,
                    ticket['type'])
                if ticket_type == 'EFR':
                    attachmenturl = urlwc
                elif ticket_type in ('ECR', 'MOM'):
                    attachmenturl = '%s/%s' % (urlwc, ticket['skill'])
                elif ticket_type in ('RF', 'PRF'):
                    attachmenturl = '%s/%s' % (urlwc, ticket['document'])
                attachmenturl = unicode_quote('%s/t%s/attachments/%s' % (
                    attachmenturl,
                    trac_id,
                    attachment_filename), '/')
                attachmenturl = '%s?action=edit&url=%s' % (
                    clickonce_app_url,
                    attachmenturl)
            else:
                # Edition from the TRAC environment
                # (see hashed_path from webdav.conf)
                webdav_protocol = attachment_edit['protocol'][suffix]
                if webdav_protocol == 'http':
                    webdav_protocol = req.scheme
                attachmenturl = '%s?action=edit&url=%s://%s/attachments/%s/ticket/%s/%s' % (
                    clickonce_app_url,
                    webdav_protocol,
                    util.get_hostname(self.env),
                    self.trac_env_name,
                    trac_id,
                    attachment_filename
                )
        else:
            label = 'View'
            # View from the TRAC environment
            attachmenturl = '%s://%s/tracs/%s/raw-attachment/ticket/%s/%s' % (
                'http',
                util.get_hostname(self.env),
                self.trac_env_name,
                trac_id,
                attachment_filename
            )

        # The button is added and replaces 'Try downloading the file instead'
        stream |= Transformer('//input[@name="attachment_edit"]').attr(
            'onclick',
            'location.href="%s"' % attachmenturl).attr(
                'value', '%s attachment' % label)

        # Tip
        if supported_suffix:
            stream |= Transformer('//input[@name="attachment_edit"]').attr(
                'title',
                '%s attachment with %s' % (label, attachment_edit['office_suite'][suffix]))

            # A checkbox is added in order to force edit mode
            if force_mode is True:
                kargs = {'type_': "checkbox",
                         'title_': "Force the edit mode",
                         'name_': "force-edit-mode",
                         'id_': "force-edit-mode",
                         'style_': 'margin-left:5px;',
                         'onclick_': ('force_edit_mode("%s", "%s", "%s")' %
                                      (self.trac_env_name, trac_id, attachment_filename))
                         }
                if checked:
                    kargs['checked_'] = 'checked'

                stream |= Transformer('//input[@name="attachment_edit"]').after(tag.input(**kargs))

                stream |= Transformer('//input[@name="force-edit-mode"]').after(tag.label(
                    u"\u2192 Edit",
                    for_="force-edit-mode",
                    title_="Force the edit mode",
                    name_="force-edit-mode-label",
                    id_="force-edit-mode-label",
                    style_="vertical-align:-40%"))

            # Tips
            if force_mode is True:
                elt_name = 'force-edit-mode-label'
            else:
                elt_name = 'attachment_edit'

            stream |= Transformer('//label[@name="%s"]' % elt_name).after(tag.a(
                'Not working ?',
                id_='config',
                name_='config',
                title_=("Click here if you're experiencing trouble"
                        " accessing the %s file" %
                        attachment_edit['office_suite'][suffix]),
                href="%s/index.php?post/76" % dc_url,
                style_='margin-left:1em; vertical-align:-40%'))

            if edit_mode is True:
                if ticket_type in tickets_with_forms:
                    tip = ("Your modified attachment will be submitted to Trac"
                           " through the ticket 'Submit changes' button")
                else:
                    tip = ("Be aware that only the last version of the "
                           "attachment you modify will be accessible")

                stream |= Transformer('//a[@name="config"]').after(tag.span(
                    tip,
                    style_="margin-left:1em; vertical-align:-40%"))

        return stream

    # IRequestFilter methods

    def pre_process_request(self, req, handler):
        """The pre-processing done when a request is submitted to TRAC """

        if req.method == 'POST':

            if 'preview' not in req.args and req.path_info.startswith('/newticket'):

                self._pre_process_newticket_post(req, handler)

            elif 'preview' not in req.args and req.path_info.startswith('/ticket'):

                self._pre_process_submitticket_post(req, handler)

            elif req.path_info.startswith('/attachment/') and req.args.get('action') == 'new':

                self._pre_process_newattachment_post(req, handler)

            elif req.path_info.startswith('/query') or req.path_info.startswith('/report'):

                self._pre_process_report_post(req, handler)

        elif req.method == 'GET':

            if 'preview' not in req.args and req.path_info.startswith('/newticket'):

                self._pre_process_newticket_get(req, handler)

            elif req.path_info.startswith('/ticket/') and not req.args.get('format') == 'rss' and 'action' not in req.args:

                self._pre_process_viewticket_get(req, handler)

            elif req.path_info.startswith('/attachment/ticket/') and req.args.get('action') == 'new' and 'selected_url' not in req.args:

                self._pre_process_newattachment_get(req, handler)

            elif 'compare' in req.args or 'compare_1' in req.args or 'compare_2' in req.args:

                self._pre_process_doc_compare_get(req, handler)

        return handler

    def _pre_process_newticket_post(self, req, handler):
        """ 'Create Ticket' button """

        now = datetime.now(localtz)
        req.args['field_submitdate'] = unicode(now.isoformat(' '))
        req.args['field_submitcomment'] = req.args.get('comment') or ''
        req.args['field_initialcomment'] = req.args['field_submitcomment']
        if req.args['field_type'] == 'DOC':
            if not req.args['field_sourceurl']:
                source_urls = [tg.source_url for tg in model.Tag.select(
                               self.env, ['tagged_item="%s"' %
                                          req.args.get('field_fromversion')],
                               tag_type='version_tags')]
                if source_urls:
                    last_path_rev_author = util.get_last_path_rev_author(self.env, util.get_url(source_urls[-1]))
                    path = last_path_rev_author[1]
                    rev = last_path_rev_author[2]
                    req.args['field_sourceurl'] = '%s?rev=%s' % (path, rev)
                else:
                    db = self.env.get_db_cnx()
                    cursor = db.cursor()
                    cursor.execute("""
                        SELECT tc.value FROM
                        ticket AS t, ticket_custom AS tc
                        WHERE t.type = 'DOC'
                        AND t.summary = '%s'
                        AND tc.ticket = t.id
                        AND tc.name = 'sourceurl'
                        """ % ('DOC_%s' % req.args.get('field_fromversion')))
                    row = cursor.fetchone()
                    if not row:
                        raise TracError(_("Can't create ticket"))
                    else:
                        req.args['field_sourceurl'] = row[0]

    def _pre_process_submitticket_post(self, req, handler):
        """ 'Submit changes' button """

        if 'id' in req.args:
            trac_id = req.args.get('id')
        else:
            raise TracError("No ticket specified")

        ticket = Ticket(self.env, trac_id)

        tickets_with_forms = set(('EFR', 'ECR', 'RF', 'PRF'))

        if 'MOM' in util.get_prop_values(self.env,
                                         'ticket_edit.office_suite').keys():
            tickets_with_forms.add('MOM')

        if ticket['type'] in tickets_with_forms:
            tp_data = form.TicketForm.get_ticket_process_data(self.env, req.authname, ticket)
            tf = tp_data['ticket_form']
            if not util.exist_in_repo(self.env, tf.http_url):
                tickets_with_forms.remove(ticket['type'])

        if ticket['type'] in tickets_with_forms:
            # Reject 'Submit' if a conflict is not yet resolved
            if os.access(tp_data['ticket_form'].minecontent_filename, os.F_OK):
                add_warning(req, _("Sorry, cannot save your changes. "
                                   "You have first to resolve the conflict."))
                req.args['preview'] = True
                return handler

        # Force detection of a change (for concurrent access handling)
        now = datetime.now(localtz)
        req.args['field_submitdate'] = unicode(now.isoformat(' '))
        req.args['field_submitcomment'] = req.args.get('comment')

    def _pre_process_newattachment_post(self, req, handler):
        """ 'Attach file' button """

        # This is POST here: attachment selected from repository browser or effective attachment adding
        if req.args.get('attachment_source') in ('repository', 'generated') and 'cancel' not in req.args:
            req.redirect(req.href.attachment(
                'ticket',
                req.args.get('path'),
                action='new',
                attachfilebutton='Attach selected file ...',
                attachment_source=req.args.get('attachment_source'),
                selected_url=req.args.get('selected_url'),
                add=req.args.get('add'),
                rename=req.args.get('rename'),
                new_attachment_filename=req.args.get('new_attachment_filename'),
                description=req.args.get('description'),
                replace=req.args.get('replace')))

    def _pre_process_report_post(self, req, handler):
        """ 'PDF print' button """

        # This is POST here: tickets selected from report for pdf printing
        if 'pdf_show' in req.args:
            if 'pdf_checkbox' in req.args:
                # Handle PDF conversion asynchronously
                pid = os.fork()
                if pid == 0:  # Child process
                    # The following waiting time is for allowing the parent process
                    # to end because it seems concurrency is poorly supported for
                    # access to the Trac database and generates the following error:
                    # "sqlite backend database disk image is malformed"
                    sleep(10)
                    form.TicketForm.tickets_pdf_convert(self.env,
                                                        self.trac_env_name,
                                                        req.authname,
                                                        req.args.get('pdf_checkbox'))
                    os._exit(0)
                else:  # Parent process
                    base_url = '/PDF-printing/%s' % self.trac_env_name
                    add_notice(req, _('You will receive an email '
                                      'when your packaging job is complete. '))
                    add_notice(req, tag(_('You may also click on '),
                                        tag.a(_('this link'),
                                              href="%s" % base_url),
                                        _(' to access the package(s) directly. '),
                                        tag.b(_('Please allow some time')),
                                        _(' for zip generation.')))
            else:
                add_warning(req, _('You have not selected any ticket for PDF printing.'))
        if req.path_info.startswith('/report') and 'action' not in req.args:
            # POST not supported by TRAC
            req.redirect(req.base_url + req.path_info + '?' + req.query_string)

    def _pre_process_newticket_get(self, req, handler):
        """ 'Create ticket' button """

        def default_type():
            # Default ticket type
            tkt_type = self.config.get('ticket', 'default_type')
            if tkt_type not in self._ticket_types:
                raise TracError('%s is the default ticket type (trac.ini) but that ticket type is not defined in the database'
                                ' (check with the trac-admin utility). Is a local definition missing ?' % tkt_type)

            # Some ticket types are not allowed if not authorized
            conf_mgmt = self.env.config.get('artusplugin', 'conf_mgmt')
            if (conf_mgmt == '1' and
                'VERSION_TAG_DELETE' not in req.perm and
                tkt_type in ('ECR', 'DOC', 'ECM')):
                # Safe default ticket type in that case
                tkt_type = 'EFR'

            return tkt_type

        ticket_type = req.args.get('type')
        if ticket_type not in self._ticket_types:
            ticket_type = default_type()
            req.redirect("%s%s?type=%s" % (req.base_url, req.path_info, ticket_type))

        if ticket_type == 'AI':
            if 'parent' in req.args:
                parent = Ticket(self.env, req.args.get('parent').strip('#'))
            else:
                parent = None
            if 'activity' not in req.args:
                activity = None
                if parent:
                    if parent['type'] == 'MOM':
                        activity = self._activity[parent['type']][parent['momtype']]
                    else:
                        activity = self._activity[parent['type']]
                if activity:
                    req.redirect("%s%s?%s&activity=%s" % (req.base_url, req.path_info, req.query_string, activity))
            elif 'milestone' not in req.args:
                milestone = None
                if parent:
                    if parent['milestonetag']:
                        milestone = model.Tag(self.env, parent['milestonetag']).tagged_item
                if milestone:
                    req.redirect("%s%s?%s&milestone=%s" % (req.base_url, req.path_info, req.query_string, milestone))

    def _pre_process_viewticket_get(self, req, handler):
        """ 'View ticket' link """

        # Anonymous users are redirected in order to authenticate
        # This is handled in admin pre_process_request
        if req.authname == "anonymous":
            return handler

        if 'id' in req.args:
            trac_id = req.args.get('id')
        else:
            raise TracError("No ticket specified")

        # 'Next ticket' makes Mozilla Firefox prefetch
        inheaders = dict(req._inheaders)
        if 'x-moz' in inheaders and inheaders['x-moz'] == 'prefetch':
            return handler

        ticket = Ticket(self.env, trac_id)

        syslog.syslog("%s(%s): Affichage ticket %s (%s)" % (self.trac_env_name, req.authname, trac_id, ticket['type']))

        tickets_with_forms = set(('EFR', 'ECR', 'RF', 'PRF'))

        if 'MOM' in util.get_prop_values(self.env,
                                         'ticket_edit.office_suite').keys():
            tickets_with_forms.add('MOM')

        if ticket['type'] in tickets_with_forms:
            tp_data = form.TicketForm.get_ticket_process_data(self.env, req.authname, ticket)
            tf = tp_data['ticket_form']
            if not util.exist_in_repo(self.env, tf.http_url):
                tickets_with_forms.remove(ticket['type'])

        if ticket['type'] in tickets_with_forms:
            # Ticket form merge done ?
            if req.args.get('ticket_form_merged') == 'True':
                ticket_form_merge = os.access(tf.minecontent_filename, os.F_OK)
                if ticket_form_merge:
                    os.remove(tf.minecontent_filename)
                    if req.args.get('forced'):
                        req.redirect(req.href.ticket(trac_id, forced=req.args.get('forced')))
                    else:
                        req.redirect(req.href.ticket(trac_id))
                else:
                    msg = tag.p("An error has occurred in ARTUS plugin at line %s. Please contact the TRAC administrator" % util.lineno())
                    req.chrome['warnings'].append(msg)
                    req.args['preview'] = True
                    return handler

            # Attachments merge done ?
            elif req.args.get('attachment_merged') == 'True':
                conflict_attachment_paths = glob.glob(tp_data['attachment'].destpath + '/*.mine')
                attachment_merge = len(conflict_attachment_paths) != 0
                if attachment_merge:
                    for path in conflict_attachment_paths:
                        os.remove(path)
                    if req.args.get('forced'):
                        req.redirect(req.href.ticket(trac_id, forced=req.args.get('forced')))
                    else:
                        req.redirect(req.href.ticket(trac_id))
                else:
                    msg = tag.p("An error has occurred in ARTUS plugin at line %s. Please contact the TRAC administrator" % util.lineno())
                    req.chrome['warnings'].append(msg)
                    req.args['preview'] = True
                    return handler

            # edit form setup
            edit_or_view_mode = util.get_edit_or_view_mode(self.env, req, ticket, 'TICKET_FORCE_EDIT')
            checked = edit_or_view_mode[2]

            # Setup of working copy
            if ticket['type'] == 'MOM' and ticket['status'] == '01-assigned_for_description' and ticket['description']:
                revision_index = ticket['description'].rfind('@')
                if revision_index != -1:
                    revision = ticket['description'][revision_index + 2:-1]
                    revision_index = ticket['description'].rfind('?')
                    if revision_index != -1:
                        path_index = ticket['description'].rfind('[')
                        if path_index != -1:
                            path = "/%s%s" % ('trunk', util.get_path(ticket['description'][path_index + 1:revision_index])[1])
                            repo_names = self.env.config.get('artusplugin', 'conf_mgmt.repo_names')
                            if repo_names:
                                path = "/%s%s" % (repo_names, path)
                            last_rev_repo = util.get_last_path_rev_author(
                                self.env, path)[2]
                            if last_rev_repo != revision:
                                # The revision has changed
                                if req.authname == ticket['owner']:
                                    def change_rev(match_obj):
                                        if match_obj.group(1) is not None:
                                            return "rev=%s" % last_rev_repo
                                        if match_obj.group(2) is not None:
                                            return "@ %s" % last_rev_repo
                                    template_link = ticket['description'][:ticket['description'].find('[[BR]]')]
                                    ticket_link = ticket['description'][ticket['description'].find('[[BR]]'):]
                                    ticket_link = re.sub(r"rev=(\d+)|@ (\d+)", change_rev, ticket_link)
                                    ticket['description'] = '%s%s' % (template_link, ticket_link)
                                    now = datetime.now(utc)
                                    ticket.save_changes('trac', _('MOM (Meeting data) revision changed to %(rev)s (on behalf of %(user)s)', rev=last_rev_repo, user=req.authname), now)
                                else:
                                    # Just a warning
                                    message = tag.p(tag.p(_('The MOM (Meeting data) revision is not up to date (last revision in the repository: %s).' % last_rev_repo)), class_="message")
                                    add_warning(req, message)

            if not os.access(tf.type_subpath, os.F_OK):
                os.makedirs(tf.type_subpath)

            #
            # Handling of ticket form
            #

            # Any modification of the edited form ?
            edit_form_modified = tf.modified()

            # Any update of the form in the repository ?
            if os.access(tf.oldcontent_filename, os.F_OK):
                unix_cmd_list = [tp_data['svn_template_cmd'] % {'subcommand': 'st --show-updates '} + '"' + tf.oldcontent_filename + '" | grep \* | awk \'{print}\'']  # awk removes the error status

                # Effective application of the list of commands
                lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())[1]

                if len(lines) != 0:  # there is an update in the repository
                    repo_form_modified = True
                else:
                    repo_form_modified = False
            else:
                repo_form_modified = True

            if os.access(tf.minecontent_filename, os.F_OK):
                # Manual merge sollicited
                # .mine file is accessible only in read-only mode (http)
                msg = tag.p("Your changes on the ",
                            tag.b("ticket form"),
                            " conflict with other changes that have been "
                            "committed in the repository in the meantime. "
                            "If you have edit right, you can resolve the "
                            "conflict by merging your changes into the "
                            "repository version (accessible through "
                            "the Edit button). "
                            "Here is the link to your changes: ",
                            tag.a('%s.%s' % (tp_data['ticket_id'],
                                             tf.mine_suffix),
                                  href=tf.webdav_url.replace(
                                      tf.webdav_protocol, 'http') +
                                  '.' + tf.mine_suffix),
                            tag.p("When you are done with the merging, "
                                  "check the following checkbox: ",
                                  tag.input(type_="checkbox",
                                            title_="Mark the merging of ticket form as done",
                                            name_="ticket_form-merging-done",
                                            id_="ticket_form-merging-done",
                                            style_='margin-left:5px;',
                                            onclick_='merging_done("%s", "%s")' % (
                                                self.trac_env_name, trac_id))))
                req.chrome['warnings'].append(msg)
                req.args['preview'] = True

            else:
                if edit_form_modified:
                    if not repo_form_modified:
                        # Commit sollicited
                        msg = tag.p("You have unsubmitted changes on the ", tag.b("ticket form"), " in your working copy - the cache used by TRAC for editing the ticket forms. "
                                    "Don't forget to submit your ticket changes if you want your ", tag.b("ticket form"), " to be committed into the Subversion repository and made available to others.")
                        req.chrome['warnings'].append(msg)
                        req.args['preview'] = True
                    else:
                        # Conflict anticipated: manual merge sollicited
                        syslog.syslog("%s(%s): Conflict anticipated on ticket form for ticket "
                                      "%s (%s): manual merge sollicited"
                                      % (self.trac_env_name, req.authname, ticket.id, ticket['type']))
                        # .mine file is accessible only in read-only mode (http)
                        msg = tag.p("Your changes on the ",
                                    tag.b("ticket form"),
                                    " conflict with other changes that have "
                                    "been committed in the repository "
                                    "in the meantime. "
                                    "If you have edit right, you can resolve "
                                    "the conflict by merging your changes into "
                                    "the commited version (accessible through "
                                    "the Edit button). "
                                    "Here is the link to your changes: ",
                                    tag.a('%s.%s' % (tp_data['ticket_id'],
                                                     tf.mine_suffix),
                                          href=tf.webdav_url.replace(
                                              tf.webdav_protocol, 'http') +
                                          '.' + tf.mine_suffix),
                                    tag.p("When you are done with the merging, "
                                          "check the following checkbox: ",
                                          tag.input(type_="checkbox",
                                                    title_="Mark the merging of ticket form as done",
                                                    name_="ticket_form-merging-done",
                                                    id_="ticket_form-merging-done",
                                                    style_='margin-left:5px;',
                                                    onclick_='merging_done("%s", "%s")' % (
                                                           self.trac_env_name, trac_id))))
                        req.chrome['warnings'].append(msg)
                        req.args['preview'] = True

                        # The edit form is pushed aside
                        copy(tf.content_filename, tf.minecontent_filename)

                        # The update is checked out
                        unix_cmd_list = [tp_data['svn_template_cmd'] % {'subcommand': 'co --depth files'} + '"' + tf.http_url + '" "' + tf.path + '"']

                        # Effective application of the list of commands
                        util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())

                        # Prepare the edit form according to the current workflow status and ticket ownership
                        tf.prepare_edit_form([ticket, tp_data, [None, 'help_on'], checked])
                else:
                    # The ticket is checked out
                    unix_cmd_list = [tp_data['svn_template_cmd'] % {'subcommand': 'co --depth files'} + '"' + tf.http_url + '" "' + tf.path + '"']

                    # Effective application of the list of commands
                    util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())

                    # Prepare the edit form according to the current workflow status and ticket ownership
                    tf.prepare_edit_form([ticket, tp_data, [None, 'help_on'], checked])

            #
            # Handling of attachments
            #

            # Any modification of attachments ?
            unix_cmd_list = [tp_data['svn_template_cmd'] % {'subcommand': 'st'} + '"' + tp_data['attachment'].destpath + '" | grep "^M" | awk -F "      " \'{print $2}\'']

            # Effective application of the list of commands
            lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())[1]

            if len(lines) != 0:  # at least one attachment has been modified
                my_attachment_paths = [line.rstrip('\n').strip() for line in lines]
                attachment_modified = True
            else:
                attachment_modified = False

            if attachment_modified:
                # Any update of those attachments in the repository ?
                unix_cmd_list = [tp_data['svn_template_cmd'] % {'subcommand': 'st --show-updates'} + '"' + '" "'.join(my_attachment_paths) + '" | grep \* | awk \'{print $4}\'']

                # Effective application of the list of commands
                lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())[1]

                if len(lines) != 0:  # there is an update in the repository
                    our_attachment_paths = [line.rstrip('\n') for line in lines]
                    repo_attachment_modified = True
                else:
                    repo_attachment_modified = False

            conflict_attachment_paths = glob.glob(tp_data['attachment'].destpath + '/*.mine')

            if len(conflict_attachment_paths) != 0:  # at least one attachment is in conflict
                # Manual merge sollicited
                conflict_attachment_filenames = [path.replace(tp_data['attachment'].destpath + '/', '') for path in conflict_attachment_paths]
                filetags = []
                # .mine files are accessible only in read-only mode (http)
                for filetag in [tag.a(filename, href=tp_data['attachment'].webdav_url.replace(tp_data['attachment'].webdav_protocol, 'http') + '/' + filename) for filename in conflict_attachment_filenames]:
                    filetags += [filetag, ' ']
                filetags.append(tag.p("When you are done with the merging, check the following checkbox: ",
                                      tag.input(type_="checkbox",
                                                title_="Mark the merging of attachments as done",
                                                name_="attachment-merging-done",
                                                id_="attachment-merging-done",
                                                style_='margin-left:5px;',
                                                onclick_='merging_done("%s", "%s")' % (self.trac_env_name, trac_id))))
                msg = tag.p("Your changes on ", tag.b("attachments"), " conflict with other changes that have been committed in the repository in the meantime. "
                            "You can resolve the conflict by merging your changes into the repository versions (accessible through the 'Edit attachment' button). "
                            "Here are the links to your changes: ", *filetags)
                req.args['preview'] = True

            else:
                if attachment_modified:
                    if not repo_attachment_modified:
                        # Commit sollicited
                        msg = tag.p("You have unsubmitted changes on ", tag.b("attachments"), " in your working copy - the cache used by TRAC for editing the ticket forms. "
                                    "Don't forget to submit your ticket changes if you want your ", tag.b("attachments"), " to be committed into the Subversion repository and made available to others.")
                        req.chrome['warnings'].append(msg)
                        req.args['preview'] = True

                        # Locally modified attachments are not modified in the repository so no merge will be done

                        # The attachments are checked out
                        unix_cmd_list = [tp_data['svn_template_cmd'] % {'subcommand': 'co --depth files'} + '"' + tp_data['attachment'].http_url + '" "' + tp_data['attachment'].destpath + '"']

                        # Effective application of the list of commands
                        util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
                    else:
                        # Conflict anticipated: manual merge sollicited
                        syslog.syslog("%s(%s): Conflict anticipated on attachments for ticket "
                                      "%s (%s): manual merge sollicited"
                                      % (self.trac_env_name, req.authname, ticket.id, ticket['type']))
                        conflict_attachment_filenames = [path.replace(tp_data['attachment'].destpath + '/', '') + '.mine' for path in our_attachment_paths]
                        filetags = []
                        # .mine files are accessible only in read-only mode (http)
                        for filetag in [tag.a(filename, href=tp_data['attachment'].webdav_url.replace(tp_data['attachment'].webdav_protocol, 'http') + '/' + filename) for filename in conflict_attachment_filenames]:
                            filetags += [filetag, ' ']
                        filetags.append(tag.p("When you are done with the merging, check the following checkbox: ",
                                              tag.input(type_="checkbox",
                                                        title_="Mark the merging of attachments as done",
                                                        name_="attachment-merging-done",
                                                        id_="attachment-merging-done",
                                                        style_='margin-left:5px;',
                                                        onclick_='merging_done("%s", "%s")' % (self.trac_env_name, trac_id))))
                        msg = tag.p("Your changes on ", tag.b("attachments"), " conflict with other changes that have been committed in the repository in the meantime. "
                                    "You can resolve the conflict by merging your changes into the repository versions (accessible through the 'Edit attachment' button). "
                                    "Here are the links to your changes: ", *filetags)
                        req.chrome['warnings'].append(msg)
                        req.args['preview'] = True

                        # The modified attachments are pushed aside
                        for path in our_attachment_paths:
                            copy(path, path + '.mine')

                        # The local modifications in conflict are cancelled
                        unix_cmd_list = [tp_data['svn_template_cmd'] % {'subcommand': 'revert'} + '"' + '" "'.join(our_attachment_paths) + '"']

                        # Effective application of the list of commands
                        util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())

                        # The update is checked out
                        unix_cmd_list = [tp_data['svn_template_cmd'] % {'subcommand': 'co --depth files'} + '"' + tp_data['attachment'].http_url + '" "' + tp_data['attachment'].destpath + '"']

                        # Effective application of the list of commands
                        util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())
                else:
                    # No attachment is locally modified so there will be no merge

                    # The attachments are checked out
                    unix_cmd_list = [tp_data['svn_template_cmd'] % {'subcommand': 'co --depth files'} + '"' + tp_data['attachment'].http_url + '" "' + tp_data['attachment'].destpath + '"']

                    # Effective application of the list of commands
                    util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())

                    # Symbolic links are created for each attachment
                    # if there are special characters in its name
                    srcpath = '/srv/trac/%s/attachments/ticket/%s' % (self.trac_env_name, trac_id)
                    if os.access(srcpath, os.F_OK):
                        env_filenames = [filepath.replace(srcpath + '/', '') for filepath in glob.glob(srcpath + '/*') if filepath != unicode_unquote(str(filepath))]
                    else:
                        env_filenames = []

                    unix_cmd_list = []
                    for env_filename in env_filenames:
                        unix_cmd_list += ['cd "' + tp_data['attachment'].destpath + '";ln -sfn "' + env_filename + '" "' + unicode_unquote(str(env_filename)) + '"']

                    # Effective application of the list of commands
                    if unix_cmd_list:
                        util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())

        elif ((ticket['type'] == 'ECM' and not Ticket_UI.get_UI(ticket).legacy) or
              ticket['type'] == 'FEE' or
              ticket['type'] == 'DOC'):
            # Prompts
            if ticket['type'] == 'DOC' and ticket['owner'] == req.authname:
                if ticket['status'] == '01-assigned_for_edition':
                    if 'sourceurl' in req.args and util.get_url(req.args['sourceurl']) != util.get_url(ticket['sourceurl']):
                        arg_paths, dirs = util.analyse_url(self.env, req.args['sourceurl'])
                        tkt_paths, dirs = util.analyse_url(self.env, ticket['sourceurl'])
                        arg_root = os.path.basename(arg_paths[0])
                        tkt_root = os.path.basename(tkt_paths[0])
                        if arg_root != tkt_root:
                            raise TracError(_('Switching from %s to %s is not allowed' % (arg_root, tkt_root)))
                        else:
                            add_notice(req, _('Go on by submitting the source url change'))
                    files = {'sourcefile': 'Source',
                             'pdffile': 'PDF'}
                    files_to_select = []
                    for fn in files.keys():
                        if not ticket[fn]:
                            files_to_select.append(files[fn])
                    if files_to_select:
                        notice = ' and '.join(files_to_select)
                        notice = 'Go on by selecting the %s file' % notice
                        if len(files_to_select) > 1:
                            notice += 's'
                        add_notice(req, _(notice))

                elif (ticket['status'] in ('02-assigned_for_peer_review',
                                           '03-assigned_for_formal_review')):
                    if not util.child_tickets_for_tag(ticket):
                        add_notice(req, _("Go on by creating and assigning "
                                          "PRF Child Ticket(s) so your document "
                                          "gets reviewed"))

            # Setup WC
            if (ticket['status'] in ('01-assigned_for_edition', '05-assigned_for_sending') and
                ticket['sourceurl']):
                last_rev_repo = util.get_last_path_rev_author(
                    self.env, util.get_url(ticket['sourceurl']))[2]
                if last_rev_repo != util.get_revision(ticket['sourceurl']):
                    # The revision has changed
                    if req.authname == ticket['owner']:
                        new_sourceurl = '%s?rev=%s' % (util.get_url(ticket['sourceurl']), last_rev_repo)
                        new_url = util.get_repo_url(self.env, new_sourceurl)
                        if util.exist_in_repo(self.env, new_url):
                            # Update ticket sourceurl automatically IF it does exist
                            ticket['sourceurl'] = new_sourceurl
                            now = datetime.now(utc)
                            ticket.save_changes('trac', _('Source Url changed (on behalf of %(user)s)', user=req.authname), now)
                            # When sourceurl changes, the sourcefile has to be checked
                            # as to its data and code and updated/upgraded if required -
                            # use case: upload out of Trac (eg TortoiseSvn)
                            template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
                            with template_cls(self.env, self.trac_env_name,
                                              req.authname, ticket) as doc:
                                if ticket['sourcefile'] and ticket['sourcefile'].endswith('.docm'):
                                    doc.checkout(ticket['sourcefile'])
                                    doc.lock(ticket['sourcefile'])
                                    doc.update_data(ticket['sourcefile'])
                                    doc.upgrade_document(ticket['sourcefile'])
                                    revision = doc.commit()
                                    if revision != '':
                                        # The locks have been automatically removed
                                        # Beware recursion and cache semaphore !
                                        ticket['sourceurl'] = '%s?rev=%s' % (
                                            util.get_url(ticket['sourceurl']),
                                            revision)
                                        now = datetime.now(utc)
                                        ticket.save_changes('trac', 'Ticket changed', now)
                                    else:
                                        # Remove the locks
                                        doc.unlock(ticket['sourcefile'])
                        else:
                            message = tag.p(tag.p(_('The source path does not exist in the HEAD revision. The document folder may have been renamed, moved or deleted. Either update the url by browsing the repository or reject or delete the %s ticket as appropriate.' % ticket['type'])), class_="message")
                            add_warning(req, message)
                            add_script_data(req, g_doc_lock_unlock_disabled='disabled')
                    else:
                        # Just a warning
                        message = tag.p(tag.p(_('The source url revision is not up to date (last revision in the repository: %s).' % last_rev_repo)), class_="message")
                        add_warning(req, message)

                if (ticket['owner'] == req.authname or 'TICKET_ADMIN' in req.perm):
                    template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
                    with template_cls(self.env, self.trac_env_name,
                                      req.authname, ticket) as doc:
                        if ticket['sourcefile'] and ticket['sourcefile'] != 'N/A':
                            doc.update(ticket['sourcefile'])
                            doc.set_access(ticket['sourcefile'])
                        if ticket['pdffile'] and ticket['pdffile'] != 'N/A':
                            doc.update(ticket['pdffile'])
                            if doc.exist_in_wc(ticket['pdffile']):
                                doc.set_access(ticket['pdffile'])
                            else:
                                src_wc_status = doc.status(ticket['sourcefile'], 'wc-status')
                                src_repos_status = doc.status(ticket['sourcefile'], 'repos-status')
                                if (src_wc_status['lock_agent'] == 'trac' and
                                    src_wc_status['lock_client'] == req.authname and
                                    src_repos_status['lock_agent'] == 'trac' and
                                    src_repos_status['lock_client'] == req.authname):
                                    # Locked
                                    doc.add(ticket['pdffile'])
                                    message = tag.p(tag.p(_('The PDF File was missing in your working copy. An empty one has been created.' ), class_="message"))
                                    add_warning(req, message)
                                else:
                                    message = tag.p(tag.p(_('The PDF File is missing in your working copy. Have you removed it from the repository ?' ), class_="message"))
                                    add_warning(req, message)

    def _pre_process_newattachment_get(self, req, handler):
        """ 'Attach file' button """

        trac_id = req.args.get('path').rstrip('/')
        ticket = Ticket(self.env, trac_id)
        if ticket['type'] == 'RF':
            db = self.env.get_db_cnx()
            try:
                # We try to point to the precise location where checklists can be found.
                # We search backwards in time for an attachment meeting the following
                # three conditions:
                # 1- The looked up attachment will have a normalized name
                # 2- The associated ticket is a RF
                # 3- The associated url will not be empty

                # We base our search only on normalized attachment names
                regular_expression = r"\ACHKLST_%s_(?:%s)(?:_(?:\w|-)+)?_"
                "(?:(?:\w|-)+)_[1-9]\d*\.(?:0|[1-9]\d*)\.(?:Draft[1-9]\d*"
                "|Proposed[1-9]\d*|Released)_\w+\.odt\Z"
                regular_expression %= (self.program_name, self.env.config.get('ticket-custom',
                                                                              'skill.options'))

                source_url = None
                cursor = db.cursor()
                cursor.execute("SELECT id, filename FROM attachment WHERE type=%s ORDER BY time DESC", (ticket.resource.realm,))
                for row in cursor:
                    tkt = Ticket(self.env, row[0])
                    filename = row[1]
                    if re.search(regular_expression, filename) and tkt['type'] == 'RF':
                        try:
                            prop = model.AttachmentCustom(self.env, (tkt.resource.realm, str(tkt.id), filename, 'source_url'), db=db)
                        except ResourceNotFound:
                            continue
                        source_url = prop.value
                        break

                # If we've identified the directory where checklists are stored in the repository,
                # we try to identify further the checklist associated with our RF.
                # This will only be possible if we've already attached a checklist
                # from THIS directory and on the SAME document.

                if source_url:
                    repos = util.get_repository(self.env, source_url)
                    if repos.has_node(source_url.rsplit('/', 1)[0]):
                        cursor = db.cursor()
                        sql = 'SELECT id, filename FROM attachment_custom WHERE type=%s and name=%s and value like %s' % ('"' + ticket.resource.realm + '"', '"source_url"', '"' + source_url.rsplit('/', 1)[0] + '%"')
                        cursor.execute(sql)
                        for row in cursor:
                            tkt = Ticket(self.env, row[0])
                            filename = row[1]
                            if re.search(regular_expression, filename) and tkt['type'] == 'RF':
                                if model.Tag(self.env, ticket['document'], db=db).tracked_item == model.Tag(self.env, tkt['document'], db=db).tracked_item:
                                    # Same doc: try to avoid browsing for pointing the appropriate checklist
                                    prop = model.AttachmentCustom(self.env, (tkt.resource.realm, str(tkt.id), filename, 'source_url'), db=db)
                                    if repos.has_node(prop.value):
                                        # Checklist does exist in HEAD revision
                                        req.redirect(req.base_url + req.path_info + '?' + req.query_string + '&selected_url=%s' % prop.value)
                                    else:
                                        # Checklist doesn't exist in HEAD revision
                                        continue
                                else:
                                    # Other doc: the search goes on
                                    continue
                        # The user has to select the appropriate checklist
                        req.redirect(req.base_url + req.path_info + '?' + req.query_string + '&selected_url=%s' % source_url.rsplit('/', 1)[0])

                # The user has to select the appropriate folder
                req.redirect(req.base_url + req.path_info + '?' + req.query_string + '&selected_url=/trunk')

            except ResourceNotFound:
                # Just ignore if new functionalities not available
                pass

    def _pre_process_doc_compare_get(self, req, handler):
        """ Doc compare """

        if req.path_info.startswith('/changeset'):
            # see changeset.py

            # retrieve arguments
            new = req.args.get('new')
            old = req.args.get('old')
            reponame = req.args.get('reponame')
            if old and '@' in old:
                old, old_path = old.split('@', 1)
            if new and '@' in new:
                new, new_path = new.split('@', 1)

            rm = RepositoryManager(self.env)
            if reponame:
                repos = rm.get_repository(reponame)
            else:
                reponame, repos, new_path = rm.get_repository_by_path(new_path)

                if old_path:
                    old_reponame, old_repos, old_path = \
                        rm.get_repository_by_path(old_path)
                    if old_repos != repos:
                        raise TracError(_("Can't compare across different "
                                          "repositories: %(old)s vs. %(new)s",
                                          old=old_reponame, new=reponame))

            if not repos:
                if reponame or (new_path and new_path != '/'):
                    raise TracError(_("Repository '%(repo)s' not found",
                                      repo=reponame or new_path.strip('/')))
                else:
                    raise TracError(_("No repository specified and no default "
                                      "repository configured."))

            # normalize and check for special case
            try:
                new_path = repos.normalize_path(new_path)
                new = repos.normalize_rev(new)
                full_new_path = unicode_quote('/' + pathjoin(repos.reponame, new_path), '/')
                old_path = repos.normalize_path(old_path or new_path)
                old = repos.normalize_rev(old or new)
                full_old_path = unicode_quote('/' + pathjoin(repos.reponame, old_path), '/')
            except NoSuchChangeset:
                e = sys.exc_info()[1]
                try:
                    raise ResourceNotFound(e.args[0], _('Invalid Changeset Number'))
                finally:
                    del e

            if old_path == new_path and old == new:  # compare with HEAD
                new = repos.youngest_rev

            # Launch diff
            o_exporturl = '%s@%s' % (util.get_repo_url(self.env, full_old_path), old)
            r_exporturl = '%s@%s' % (util.get_repo_url(self.env, full_new_path), new)
            clickonce_appurl = self.env.config.get('artusplugin', 'clickonce_app_url_MS_Office')
            diffurl = '%s?action=compare&url=%s&url=%s' % (clickonce_appurl, o_exporturl, r_exporturl)
            req.redirect(diffurl)

        elif req.path_info.startswith('/ticket'):
            """
              Return diff url for comparing source files:

              case 'compare_1': between previous and current source files
              case 'compare_2': between original and modified source files
            """

            def get_previous_PRF_tkid(ticket):
                match = re.match(r'.+\(previously reviewed through #(\d+)\):',
                                 ticket['description'])
                if not match:
                    # Legacy
                    match = re.match(r'.+\(Previously reviewed through #(\d+)\):',
                                 ticket['description'])
                    if not match:
                        raise TracError("Cannot compare: "
                                        "could not determine "
                                        "previous PRF ticket id"
                                    )
                return match.group(1)

            def get_tagged_sourceurl_sourcefile(ticket, tagname):
                '''
                  Return sourceurl and sourcefile for a given tagname of a given DOC ticket

                :param ticket: DOC/ECM/FEE ticket
                :param tagname: DOC/ECM/FEE tag name
                '''
                # Check DOC/ECM/FEE tag has been applied through the DOC/ECM/FEE ticket and
                # the source file is a Word file
                ticket_module = TicketModule(self.env)
                changes = [change for change in
                           ticket_module.
                           rendered_changelog_entries(req, ticket)]
                found_tag = False
                sourcefile = None
                applied_regexp = '^Tag (.+) applied$'
                for change in reversed(changes):
                    if not found_tag:
                        match = re.search(applied_regexp, change['comment'])
                        if match:
                            tg = match.group(1)
                            if tg == tagname:
                                found_tag = True
                    else:
                        # This is the value of sourcefile BEFORE tag was applied
                        if 'sourcefile' in change['fields']:
                            sourcefile = change['fields']['sourcefile']['new']
                            break

                if not found_tag:
                    raise TracError(tag.p("Cannot compare: Tag ", tag.a("%s" % tagname, href=req.href.admin('tags_mgmt', 'version_tags', tagname)),
                                          " was not applied through ticket ", tag.a("#%s" % ticket.id, href=req.href.ticket(ticket.id))))
                else:
                    if not sourcefile or not util.is_word_file(sourcefile):
                        raise TracError("Cannot compare: "
                                        "source file (%s) "
                                        "is not a Word file"
                                        % sourcefile)

                vtg = model.Tag(self.env, name=tagname)
                sourceurl = util.get_repo_url(self.env, vtg.source_url)

                return sourceurl, sourcefile

            def get_modified_sourceurl(ticket):
                '''
                  Return modified sourceurl for a given PRF ticket

                :param ticket: PRF ticket
                '''
                match = re.match(r'.+\(modified\): '
                                 '\[/browser(/.+) .+ @ \d+\]',
                                 ticket['description'])
                if not match:
                    # Legacy
                    match = re.match(r'.+\(Verification modified input\): '
                                 '\[/browser(/.+) .+ @ \d+\]',
                                 ticket['description'])
                    if not match:
                        raise TracError("Cannot compare: "
                                        "could not determine "
                                        "revised document url"
                                        )
                sourceurl = util.get_repo_url(self.env, match.group(1))

                return sourceurl

            PRF_tkid = req.args.get('ticket_id')
            PRF_ticket = Ticket(self.env, PRF_tkid)
            Parent_tkid = PRF_ticket['parent'].lstrip('#')
            Parent_ticket = Ticket(self.env, Parent_tkid)
            if Parent_ticket['type'] not in ('DOC', 'ECM', 'FEE'):
                raise TracError(tag.p("Cannot compare: Parent ticket ",
                                      tag.a("#%s" % Parent_tkid, href=req.href.ticket(Parent_tkid)),
                                      " is not a DOC/ECM/FEE ticket"))

            if 'compare_1' in req.args:
                # comparison between previous and current source files
                p_PRF_tkid = get_previous_PRF_tkid(PRF_ticket)
                p_PRF_ticket = Ticket(self.env, p_PRF_tkid)
                p_Parent_tkid = p_PRF_ticket['parent'].lstrip('#')
                p_Parent_ticket = Ticket(self.env, p_Parent_tkid)
                f_sourceurl, f_sourcefile = get_tagged_sourceurl_sourcefile(p_Parent_ticket, p_PRF_ticket['document'])
                if 'modified' in p_PRF_ticket['description']:
                    f_sourceurl = get_modified_sourceurl(p_PRF_ticket)
                t_sourceurl, t_sourcefile = get_tagged_sourceurl_sourcefile(Parent_ticket, PRF_ticket['document'])
            elif 'compare_2' in req.args:
                # comparison between original and modified source files
                f_sourceurl, f_sourcefile = get_tagged_sourceurl_sourcefile(Parent_ticket, PRF_ticket['document'])
                t_sourceurl = get_modified_sourceurl(PRF_ticket)
                t_sourcefile = f_sourcefile
            else:
                raise TracError(tag.p("Cannot compare: compare type not found"))

            # Launch diff
            f_exporturl = unicode_quote('%s/%s@%s' % (util.get_url(f_sourceurl), f_sourcefile, util.get_revision(f_sourceurl)), '/@')
            t_exporturl = unicode_quote('%s/%s@%s' % (util.get_url(t_sourceurl), t_sourcefile, util.get_revision(t_sourceurl)), '/@')
            clickonce_appurl = self.env.config.get('artusplugin', 'clickonce_app_url_MS_Office')
            diffurl = '%s?action=compare&url=%s&url=%s' % (clickonce_appurl, f_exporturl, t_exporturl)
            req.redirect(diffurl)

    def change_history_filter(self, ticket, data):
        changes = data['changes']

        # changes are presented up to the ticket version
        version = ticket.resource.version
        if version:
            filtered_changes = []
            for chg in changes:
                if 'cnum' in chg:
                    if chg['cnum'] <= version:
                        filtered_changes.append(chg)
                        if chg['cnum'] == version:
                            break
                else:
                    filtered_changes.append(chg)
            changes = filtered_changes

        # changes followed by a repository commit are flagged
        if ticket['type'] != 'DOC' and (ticket['type'] != 'ECM' or Ticket_UI.get_UI(ticket).legacy) and ticket['type'] != 'FEE':
            for idx, change in enumerate(changes):
                if 'cnum' in change:
                    if idx > 0 and 'description' in changes[idx - 1]['fields']:
                        change['ticket_version'] = str(int(change['cnum']) + 1)
                    else:
                        change['ticket_version'] = change['cnum']

        # changes by trac or on internal fields are filtered out
        changes_to_remove = []
        change_fields = Ticket_UI.get_UI(ticket)(self, ticket).ticket_change_fields()
        for change in changes:
            fields_to_remove = []
            for field_name, field in change['fields'].items():
                if (field_name not in change_fields.keys() or
                    field_name in ['submitcomment',
                                   'submitdate',
                                   'description',
                                   'documenturl',
                                   'signer',
                                   'sourceurl']):
                    fields_to_remove.append(field_name)
            for field_name in fields_to_remove:
                del change['fields'][field_name]
            if (len(change['fields']) == 0 and
                (not change['comment'] or
                 (change['comment'] in ['Ticket created',
                                        'Ticket changed',
                                        'Attachment(s) changed',
                                        'Document changed'] or
                  change['comment'].startswith('Attachment added') or
                  change['comment'].startswith('Attachment deleted') or
                  change['comment'].startswith('Source Url changed')))):
                changes_to_remove.append(change)
            else:
                for field_name, field in change['fields'].items():
                    del change['fields'][field_name]
                    change['fields'][change_fields[field_name]] = field
        for change in changes_to_remove:
            changes.remove(change)

        # permanent changes
        if ticket['type'] != 'DOC' and (ticket['type'] != 'ECM' or Ticket_UI.get_UI(ticket).legacy) and ticket['type'] != 'FEE':
            changes = [change for change in changes
                       if change['permanent'] == 1]

        data['changes'] = changes

    def post_process_request(self, req, template, data, content_type):
        """The post-processing done when a request is submitted to TRAC
           This is used for patching data before template computing
           when processing is done by TRAC itself """

        def attachments_can(req, ticket, ovalue):
            return (ovalue and
                    (('TICKET_ADMIN' in req.perm or
                      req.authname == ticket['owner']) and
                     (ticket['type'] not in ('EFR', 'ECR') or
                      (ticket['type'] in ('EFR', 'ECR') and
                       (ticket['status'] == '01-assigned_for_description' or
                        ticket['status'] == '03-assigned_for_analysis' or
                        ticket['status'] == '05-assigned_for_implementation' or
                        ticket['status'] == '07-assigned_for_closure_actions')
                       ))))

        if data is None:
            data = {}

        data['wiki_url'] = '%s/wiki' % self.env.config.get('trac', 'base_url')
        data['dc_url'] = self.env.config.get('artusplugin', 'dc_url')
        data['caller'] = req.args.get('caller', None) if isinstance(req.args.get('caller'), basestring) else None

        # Attachment ticket id ('attachment_tid') support:
        #   the browser is used for changing the url or the revision
        if 'attachment_tid' in req.args:
            data['attachment_tid'] = req.args.get('attachment_tid')
        else:
            data['attachment_tid'] = None
        data['url_add_params'] = util.url_add_params

        if template in ['ticket.html', 'ticket_preview.html',
                        'ticket_delete.html', 'ticket_box.html']:
            ticket = data['ticket']
            # Customization data for displaying ticket fields
            ticket_UI = Ticket_UI.get_UI(ticket)(self, ticket)
            data['ticket_UI'] = ticket_UI
            data['ticketbox_fields'] = ticket_UI.ticketbox_fields(req.perm)
            data['fields_properties'] = ticket_UI.fields_properties(req.perm)
            data['get_branch'] = util.get_branch

            if ticket.exists:
                # ATTACHMENT_CREATE privilege restricted by ticket ownership and
                # ticket workflow state:
                # ownership is required and validation states are excluded
                data['attachments']['can_create'] = attachments_can(
                    req,
                    ticket,
                    data['attachments']['can_create'])
                if ticket['type'] == 'DOC' and 'VERSION_TAG_VIEW' in req.perm:
                    # Contextual links
                    ecm_report = self.env.config.get('artusplugin', 'ECM_report')
                    add_ctxtnav(req, _('View Associated Delivery ECMs'),
                                href=('%s/query?type=ECM&description=~%s%s.&ecmtype=Document+Delivery&group=ecmtype'
                                      '&col=id&col=summary&col=owner&col=status&col=milestone&col=resolution'
                                      '&col=keywords&col=time&col=pdffile&report=%s&order=id&row=description') %
                                    (req.base_path, ticket['configurationitem'], ticket['versionsuffix'], ecm_report))
                    add_ctxtnav(req, _('View Associated Document'),
                                href='%s/admin/tags_mgmt/documents/%s' %
                                    (req.base_path, ticket['configurationitem']))
                elif ticket['type'] in ('RF', 'PRF') and 'VERSION_TAG_VIEW' in req.perm:
                    # New contextual link
                    if ticket['parent']:
                        tkt = Ticket(self.env, ticket['parent'].lstrip('#'))
                        if tkt['type'] == 'DOC':
                            tg = model.Tag(self.env, ticket['document'])
                            add_ctxtnav(req, _('View Associated Document'),
                                        href='%s/admin/tags_mgmt/documents/%s' %
                                            (req.base_path, tg.tracked_item))
                if ticket.exists and ((ticket['type'] == 'ECM' and not ticket_UI.legacy) or ticket['type'] == 'FEE' or ticket['type'] == 'DOC'):
                    if ticket['type'] == 'DOC':
                        source_types = util.get_prop_values(self.env, "source_types")
                        sourcetype = ticket['sourcetype']
                        data['automation'] = source_types[sourcetype].split('||')[5].strip()
                    else:
                        data['automation'] = 'full'

        if template == 'ticket_delete.html' and data['action'] == 'delete':
            ticket = data['ticket']
            # Validate that no lock is set before deleting the ticket
            if (ticket['type'] == 'ECM' and not Ticket_UI.get_UI(ticket).legacy) or ticket['type'] == 'FEE' or ticket['type'] == 'DOC':
                template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
                with template_cls(self.env,
                                  self.trac_env_name,
                                  req.authname,
                                  ticket) as doc:
                    # Check existence of source or pdf trac lock in the repository
                    # This is to avoid deleting a ticket in Lock status
                    for docfile in (ticket['sourcefile'], ticket['pdffile']):
                        repos_status = doc.status(docfile, 'repos-status')
                        if repos_status['lock_agent'] == 'trac':
                            message = (_("Sorry, cannot delete the ticket because the file %(file)s is locked by %(agent)s "
                                       "(on behalf of %(client)s)", file=docfile, agent=repos_status['lock_agent'], client=repos_status['lock_client']))
                            raise TracError(message)
            elif ticket['type'] == 'MOM' and 'MOM' in util.get_prop_values(self.env, 'ticket_edit.office_suite').keys():
                tp_data = form.TicketForm.get_ticket_process_data(self.env, req.authname, ticket)
                tf = tp_data['ticket_form']
                # Check existence of form lock in the repository
                # This is to avoid deleting a ticket in Lock status
                repos_status = tf.status('repos-status')
                if repos_status['lock_agent']:
                    message = _("Sorry, cannot delete the ticket because the associated form is locked by %(agent)s", repos_status['lock_agent'])
                    if repos_status['lock_client']:
                        message += _(" (on behalf of %(user)s)", user=repos_status['lock_client'])
                    raise TracError(message)
                else:
                    add_warning(req, _('The form data in the repository will be deleted.'))

            # Ticket delete (not ticket comment delete)
            if ticket['type'] in ('EFR', 'ECR', 'RF', 'PRF', 'MOM', 'DOC', 'ECM', 'FEE'):
                add_warning(req, _('All working copies associated to this ticket will be deleted.'))

            if (ticket['type'] in ('EFR', 'ECR', 'AI', 'RISK', 'MEMO') or
                (ticket['type'] == 'ECM' and Ticket_UI.get_UI(ticket).legacy) or
                (ticket['type'] == 'MOM' and ticket['momtype'] == 'Audit')):
                add_warning(req, _('The ticket type chronological number will be decremented.'))

        if template in ['ticket.html', 'ticket_preview.html']:
            ticket = data['ticket']

            # Computing of which action are allowed
            workflow = self.action_controllers[0]
            ticket_wf = workflow.get_ticket_wf(ticket)(workflow, req, ticket)

            def get_workflow_required_permission(action):
                return ticket_wf.get_required_permission(action)

            def get_workflow_required_role(action):
                return ticket_wf.get_required_role(action)

            data['get_workflow_required_permission'] = get_workflow_required_permission
            data['get_workflow_required_role'] = get_workflow_required_role
            data['author'] = ticket_wf.get_author()
            data['ticket_owner'] = ticket_wf.get_owner()
            data['ticket_status'] = ticket_wf.get_status(ticket)
            data['ticket_resolution'] = ticket_wf.get_resolution()

            def filter1(field, owners):
                if field['name'] == 'owner':
                    field['optional'] = False
                    field['options'] = owners
                elif (field['name'] == 'type'):
                    conf_mgmt = self.env.config.get('artusplugin', 'conf_mgmt')
                    if conf_mgmt == '1' and 'VERSION_TAG_DELETE' not in req.perm:
                        # authorized profile required for these ticket types
                        if 'ECR' in self._ticket_types:
                            field['options'].remove('ECR')
                        if 'DOC' in self._ticket_types:
                            field['options'].remove('DOC')
                        if 'ECM' in self._ticket_types:
                            field['options'].remove('ECM')
                elif (field['name'] == 'skill'):
                    if 'EXT' in field['options']:
                        field['options'].remove('EXT')
                return field

            def filter2(field, skills, configurationitem, fromversion, milestone):
                if (field['name'] == 'skill'):
                    field['options'] = skills
                elif field['name'] == 'configurationitem':
                    # type is declared as text in trac.ini
                    # to elude validation by TRAC
                    field['type'] = 'select'
                    field['options'] = [configurationitem]
                    field['optional'] = False
                elif field['name'] == 'fromversion':
                    # type is declared as text in trac.ini
                    # to elude validation by TRAC
                    field['type'] = 'select'
                    if not isinstance(fromversion, list):
                        fromversion = [fromversion]
                    field['options'] = fromversion
                    field['optional'] = False
                elif field['name'] == 'milestone':
                    field['value'] = milestone
                    field['optional'] = True

                return field

            def get_template_fn(source_types, sourcetype):
                tmpl_fn = source_types[sourcetype].split('||')[0].strip()
                if tmpl_fn:
                    for template_dir in Chrome(self.env).get_templates_dirs():
                        template = '%s/%s' % (template_dir, tmpl_fn)
                        if os.path.exists(template):
                            break
                    else:
                        tmpl_fn = None
                else:
                    tmpl_fn = None
                return tmpl_fn

            def filter3(field, sourceurl):
                if field['name'] == 'sourceurl':
                    # Source url update
                    field['value'] = sourceurl
                elif field['name'] == 'sourcefile':
                    if ticket['type'] == 'DOC':
                        field["type"] = "select"
                        source_types = util.get_prop_values(self.env, "source_types")
                        sourcetype = ticket['sourcetype']
                        if sourcetype in source_types and source_types[sourcetype]:
                            automation = source_types[sourcetype].split('||')[5].strip()
                            if automation != 'none':
                                # Source files @ sourceurl
                                template_fn = get_template_fn(source_types, sourcetype)
                                if template_fn:
                                    sourcefile = "%s%s" % (ticket["configurationitem"], splitext(template_fn)[1])
                                else:
                                    sourcefile = None
                                src_files = cache.PDFPackage.get_src_files(
                                    self.env, sourceurl, True, sourcefile
                                )

                                for src_file in src_files:
                                    if src_file[0] == sourcefile:
                                        break
                                else:
                                    src_files = [(f[0], False) for f in src_files]
                                    if sourcefile:
                                        src_files.append((sourcefile, True))

                                field["options"] = sorted(dict(src_files).keys())
                                if field["options"]:
                                    if (
                                        ticket["sourcefile"]
                                        and ticket["sourcefile"] in field["options"]
                                    ):
                                        field["value"] = ticket["sourcefile"]
                                    else:
                                        selected = [f[0] for f in src_files if f[1]]
                                        if selected:
                                            field["value"] = selected[0]
                                        else:
                                            field["value"] = field["options"][0]
                                else:
                                    field['options'] = ['N/A']
                                    field['value'] = 'N/A'
                            else:
                                field['options'] = ['N/A']
                                field['value'] = 'N/A'
                        else:
                            field['options'] = ['N/A']
                            field['value'] = 'N/A'
                    elif (ticket['type'] == 'ECM' and not Ticket_UI.get_UI(ticket).legacy) or ticket['type'] == 'FEE':
                        field['options'] = [ticket['sourcefile']]
                        field['value'] = ticket['sourcefile']
                elif field['name'] == 'pdffile':
                    if ticket['type'] == 'DOC':
                        field['type'] = 'select'
                        source_types = util.get_prop_values(self.env, "source_types")
                        sourcetype = ticket['sourcetype']
                        if sourcetype in source_types and source_types[sourcetype]:
                            automation = source_types[sourcetype].split('||')[5].strip()
                            pdfsigned = source_types[sourcetype].split('||')[1].strip()
                            if automation != 'none' or pdfsigned == 'true':
                                # PDF files @ sourceurl
                                template_fn = get_template_fn(source_types, sourcetype)
                                if template_fn:
                                    pdffile = "%s.pdf" % ticket["configurationitem"]
                                    if ticket["sourcefile"] and ticket["sourcefile"] != "N/A":
                                        sourcefile = ticket["sourcefile"]
                                    else:
                                        sourcefile = "%s.docm" % ticket["configurationitem"]
                                else:
                                    pdffile = None
                                    sourcefile = None
                                # We don't take PDF revision into account
                                pdf_files = cache.PDFPackage.get_pdf_files(
                                    self.env, sourceurl, True, sourcefile, -1
                                )

                                for pdf_file in pdf_files:
                                    if pdf_file[0] == pdffile:
                                        break
                                else:
                                    pdf_files = [(f[0], False) for f in pdf_files]
                                    if pdffile:
                                        pdf_files.append((pdffile, True))

                                field["options"] = sorted(dict(pdf_files).keys())
                                if field["options"]:
                                    if (
                                        ticket["pdffile"]
                                        and ticket["pdffile"] in field["options"]
                                    ):
                                        field["value"] = ticket["pdffile"]
                                    else:
                                        selected = [f[0] for f in pdf_files if f[1]]
                                        if selected:
                                            field["value"] = selected[0]
                                        else:
                                            field["value"] = field["options"][0]
                                else:
                                    field['options'] = ['N/A']
                                    field['value'] = 'N/A'
                            else:
                                field['options'] = ['N/A']
                                field['value'] = 'N/A'
                        else:
                            field['options'] = ['N/A']
                            field['value'] = 'N/A'
                    elif (ticket['type'] == 'ECM' and not Ticket_UI.get_UI(ticket).legacy) or ticket['type'] == 'FEE':
                        field['options'] = [ticket['pdffile']]
                        field['value'] = ticket['pdffile']

                return field

            def filter4(field, skill):
                if field['name'] == 'milestone':
                    # Milestone filtered according to skill
                    for optgroup in field['optgroups']:
                        optgroup['options'] = util.get_filtered_items(
                            self.env,
                            optgroup['options'],
                            util.get_milestone_skills(self.env, skill))
                return field

            def filter5(field_properties):
                field_name, properties = field_properties
                if field_name in ('sourcefile', 'pdffile'):
                    attributes, label, tip = properties
                    modif_attr = []
                    for attr in attributes:
                        if attr[0] == 'style':
                            attr = attr[0], '%s;%s' % (
                                attr[1].rstrip(';'),
                                'background-color:#f4f4f4')
                        modif_attr.append(attr)
                    modif_attr.append(('readonly', 'true'))
                    properties = modif_attr, label, tip
                return (field_name, properties)

            def filter6(field, value):
                if field['name'] in ('fromecm', 'fromfee', 'evolref'):
                    # type is declared as text in trac.ini
                    # to elude validation by TRAC
                    field['type'] = 'select'
                    field['options'] = [value]
                    field['optional'] = False

                return field

            # Set the owners list
            if not ticket.exists:
                owners = ticket_wf.get_owners_list()
                if ticket['type'] == 'PRF' and ticket['parent'] and ticket_wf.with_independence():
                    # Exclude DOC ticket owner if the only author in peer review
                    # Exclude ticket authors in formal review
                    try:
                        parent_id = ticket['parent'].strip('#')
                        parent_tkt = Ticket(self.env, int(parent_id))
                        authors = workflow.get_ticket_wf(parent_tkt).get_authors(parent_tkt)
                        if parent_tkt['status'] == '02-assigned_for_peer_review':
                            owners = [owner for owner in owners if len(authors) > 1 or owner != parent_tkt['owner']]
                        elif parent_tkt['status'] == '03-assigned_for_formal_review':
                            owners = [owner for owner in owners if owner not in authors]
                    except Exception:
                        pass
                if ticket['type'] == 'PRF':
                    # Exclude reviewers of current tag
                    try:
                        db = self.env.get_db_cnx()
                        cursor = db.cursor()
                        cursor.execute("SELECT id FROM ticket, ticket_custom WHERE id=ticket AND type='PRF' AND "
                                       "name='document' AND value='%s'" % ticket['document'])
                        prf_tkts = [tkt for tkt in [Ticket(self.env, n) for n in [int(row[0]) for row in cursor]]
                                    if tkt['status'] != 'closed' or tkt['resolution'] != 'rejected']
                        reviewers = [workflow.get_ticket_wf(tkt)(workflow, req, tkt).get_reviewer() for tkt in prf_tkts]
                        owners = [owner for owner in owners if owner not in reviewers]
                    except Exception:
                        pass

                data['fields'] = [filter1(field, owners) for field in data['fields']]

            # History filter
            if ticket.exists:
                self.change_history_filter(ticket, data)

            if (ticket['type'] == 'DOC' and
                self.env.config.get('artusplugin', 'conf_mgmt') == '1'):

                    if not ticket.exists:
                        skills = self.env.config.get('artusplugin', 'DOC_skills', '').split('|')
                        if req.method == 'POST':
                            configurationitem = req.args.get('field_configurationitem', '')
                            fromversion = req.args.get('field_fromversion', '')
                            milestone = req.args.get('field_milestone', '')
                        else:
                            configurationitem = req.args.get('configurationitem', '')
                            if configurationitem:
                                sourceurl = req.args.get('sourceurl')
                                if sourceurl:
                                    paths = util.analyse_url(self.env, util.get_url(sourceurl))[0]
                                    basename = os.path.basename(paths[0])
                                    if basename == 'branches':
                                        branch = os.path.basename(paths[1])
                                    fromversion = (req.args.get('fromversion') or
                                                   (basename == 'trunk' and 'New Document') or
                                                   (basename == 'branches' and 'New Branch Document (%s)' % branch) or
                                                   '')
                                else:
                                    fromversion = ''
                            else:
                                fromversion = ''
                            milestone = req.args.get('milestone', '')

                        data['fields'] = [filter2(field,
                                                  skills,
                                                  configurationitem,
                                                  fromversion,
                                                  milestone)
                                          for field in data['fields']]

                    else:
                        sourceurl = (req.args.get('sourceurl') or
                                     ticket.get_value_or_default('sourceurl'))
                        if req.authname != ticket['owner']:
                            last_rev_repo = util.get_last_path_rev_author(
                                self.env, util.get_url(sourceurl))[2]
                            if last_rev_repo != util.get_revision(sourceurl):
                                data['last_rev_repo'] = last_rev_repo
                        if (ticket['status'] == '01-assigned_for_edition'):
                            data['fields'] = [filter3(field, sourceurl)
                                              for field in data['fields']]
                        data['fields'] = [filter4(field, ticket['skill'])
                                          for field in data['fields']]
                        if ticket['status'] != '01-assigned_for_edition':
                            data['fields_properties'] = [
                                filter5(field_properties)
                                for field_properties in
                                data['fields_properties']]

            if (((ticket['type'] == 'ECM' and not Ticket_UI.get_UI(ticket).legacy) or
                  ticket['type'] == 'FEE') and
                self.env.config.get('artusplugin', 'conf_mgmt') == '1'):

                    if not ticket.exists:

                        values = {}
                        if req.method == 'POST':
                            values['fromecm'] = req.args.get('field_fromecm', '')
                            values['fromfee'] = req.args.get('field_fromfee', '')
                            values['evolref'] = req.args.get('field_evolref', '')
                            values['customer'] = req.args.get('field_customer', '')
                            values['program'] = req.args.get('field_program', '')
                            values['application'] = req.args.get('field_application', '')
                        else:
                            values['fromecm'] = req.args.get('fromecm', '')
                            values['fromfee'] = req.args.get('fromfee', '')
                            values['evolref'] = req.args.get('evolref', '')                            
                            values['customer'] = req.args.get('customer', '')
                            values['program'] = req.args.get('program', '')
                            values['application'] = req.args.get('application', '')
                        data['fields'] = [filter6(field, values[field['name']]
                                            if field['name'] in values else field)
                                            for field in data['fields']]

                    else:
                        sourceurl = (req.args.get('sourceurl') or
                                     ticket.get_value_or_default('sourceurl'))
                        if req.authname != ticket['owner']:
                            last_rev_repo = util.get_last_path_rev_author(
                                self.env, util.get_url(sourceurl))[2]
                            if last_rev_repo != util.get_revision(sourceurl):
                                data['last_rev_repo'] = last_rev_repo
                        if (ticket['status'] == '01-assigned_for_edition'):
                            data['fields'] = [filter3(field, sourceurl)
                                              for field in data['fields']]
                        if ticket['status'] != '01-assigned_for_edition':
                            data['fields_properties'] = [
                                filter5(field_properties)
                                for field_properties in
                                data['fields_properties']]

            if ticket['type'] in ('RF', 'PRF'):

                # PRF milestones
                if ticket['document']:
                    skill = util.get_skill(self.env, ticket['document'],
                                           self.program_name)
                    if skill == 'EXT':
                        # Try and get real skill from repository path
                        skills = util.get_prop_values(self.env, 'skill_dirs')
                        regexp = '(%s)' % '|'.join(skills.values())
                        match = re.search(regexp, ticket['documenturl'])
                        if match:
                            reversed_skills = dict(zip(skills.values(), skills.keys()))
                            skill = reversed_skills[match.group(1)]
                else:
                    skills = self.env.config.get('ticket-custom', 'skill.options').split('|')
                    skill = self.env.config.get('artusplugin', 'default_skill', 'SYS')

                if (not ticket['document'] or
                    not (ticket['document'].startswith('ECM_') or
                         ticket['document'].startswith('FEE_'))):
                    data['fields'] = [filter4(field, skill)
                                      for field in data['fields']]

                # Doc compare buttons display
                data['trac_env_name'] = self.trac_env_name
                data['doc_compare_1'] = False
                if ticket['parent'] and ticket['description']:
                    # Previous and current compared
                    if ('(previously' in ticket['description']):
                        data['doc_compare_1'] = True
                data['doc_compare_2'] = False
                if ticket['parent'] and ticket['description']:
                    # Original and modified compared
                    if 'modified' in ticket['description']:
                        data['doc_compare_2'] = True

            # RISK milestones
            if ticket.exists and ticket['type'] in ('ECR', 'AI', 'RISK'):

                skill = util.get_skill(self.env, ticket['summary'],
                                       '%s_%s' % (ticket['type'], self.program_name))

                data['fields'] = [filter4(field, skill)
                                  for field in data['fields']]

        if template in ['browser.html',
                        'revisionlog.html']:
            # Path
            for path_link in data['path_links']:
                path_link['href'] = util.url_add_params(path_link['href'], [('caller', data['caller']), ('attachment_tid', data['attachment_tid'])])

        if template in ['browser.html']:
            # 'Last Change' / 'Revision Log'
            util.entries_add_params(req, [('caller', data['caller']), ('attachment_tid', data['attachment_tid'])])
            # 'Up'
            links = req.chrome.get('links')
            if 'up' in links:
                links['up'][0]['href'] = util.url_add_params(links['up'][0]['href'], [('caller', data['caller']), ('attachment_tid', data['attachment_tid'])])

        if template in ['revisionlog.html',
                        'changeset.html']:
            # 'View Latest Revision' / 'Previous Change' / 'Next Change'
            util.entries_add_params(req, [('caller', data['caller']), ('attachment_tid', data['attachment_tid'])])

            def my_dateinfo(date):
                absolute = user_time(req, format_datetime, date)
                relative = pretty_timedelta(date)
                dateinfo_format = req.session.get('dateinfo',
                                                  Chrome(self.env).default_dateinfo_format)
                if dateinfo_format == 'absolute':
                    label = absolute
                    title = _("See timeline %(relativetime)s ago",
                              relativetime=relative)
                else:
                    label = relative
                    title = _("See timeline at %(absolutetime)s",
                              absolutetime=absolute)
                timeline_module = TimelineModule(self.env)
                link_elt = timeline_module.get_timeline_link(req, date, label, precision='second', title=title)
                link_elt(href=util.url_add_params(link_elt.attrib.get('href'), [('caller', data['caller']), ('attachment_tid', data['attachment_tid'])]))
                return link_elt
            data['my_dateinfo'] = my_dateinfo

        if template in ['timeline.html']:
            # Some internal events are filtered-out
            events = []
            for event in data['events']:
                if hasattr(event['event'][3][0], 'realm'):
                    realm = event['event'][3][0].realm
                    if realm == 'ticket':
                        # first filter
                        comment = event['event'][3][8]
                        if comment:
                            if (comment in ['Ticket created',
                                            'Ticket changed',
                                            'Attachment(s) changed',
                                            'Document changed'] or
                                comment.startswith('Attachment added') or
                                comment.startswith('Attachment deleted') or
                                comment.startswith('Source Url changed')):
                                continue
                        # ticket creation comment added to associated event
                        verb = event['event'][3][1]
                        if verb == 'created':
                            list_1 = list(event['event'])
                            list_2 = list(list_1[3])
                            ticket = event['event'][3][0]
                            list_2[8] = Ticket(self.env, ticket.id)['initialcomment']
                            list_1[3] = tuple(list_2)
                            event['event'] = tuple(list_1)
                        # second filter
                        if event['kind'] == 'editedticket':
                            info = event['event'][3][2]
                            if info:
                                raw_fields = []
                                default_language = self.env.config.get('trac', 'default_language')
                                if default_language == 'fr':
                                    plain_text = util.strip_accents(info.generate().render(TextSerializer, strip_markup=True, encoding=None))
                                    match = re.search('\AProprietes? (.*?) modifiees?\Z', plain_text)
                                    if match:
                                        raw_fields = match.group(1).split(', ')
                                elif default_language == 'en':
                                    raw_fields = plaintext(str(info))[:-len(' changed')].split(', ')
                                filtered_fields = []
                                for field in raw_fields:
                                    if field in ['Submit Comment',
                                                 'Submit Date',
                                                 'Authname',
                                                 'Description',
                                                 'Documenturl',
                                                 'Signer']:
                                        continue
                                    else:
                                        filtered_fields.append(field)
                                if len(filtered_fields) == 0:
                                    continue
                                else:
                                    if default_language == 'fr':
                                        plural = 's' if len(filtered_fields) > 1 else ''
                                        info = tag(u'Propriété%s ' % plural, [[tag.i(f), ', '] for f in filtered_fields[:-1]],
                                                    tag.i(filtered_fields[-1]), u' changée%s' % plural, tag.br())
                                    elif default_language == 'en':
                                        info = tag([[tag.i(f), ', '] for f in filtered_fields[:-1]],
                                                    tag.i(filtered_fields[-1]), ' changed', tag.br())
                                    list_1 = list(event['event'])
                                    list_2 = list(list_1[3])
                                    list_2[2] = info
                                    list_1[3] = tuple(list_2)
                                    event['event'] = tuple(list_1)

                events.append(event)
            # re-sort global list
            events = sorted(events, key=lambda e: e['date'], reverse=True)
            data['events'] = events

            # For new tickets, display the 'keywords' field instead of the description field
            # This is code hijacking from trac/ticket/web_ui.py:render_timeline_event()
            def my_render_timeline_event(context, field, event):
                if event['kind'] in ('newticket', 'editedticket', 'closedticket'):
                    if field == 'description':
                        idx = [0, 1, 2, 4, 8]
                        ticket, verb, info, status, comment = (event['event'][3][i] for i in idx)
                        descr = message = ''
                        if status == 'new':
                            keywords = Ticket(self.env, ticket.id)['keywords']
                            if keywords:
                                descr = tag(keywords, tag.br())
                        else:
                            descr = info
                        message = comment
                        t_context = context(resource=ticket)
                        t_context.set_hints(preserve_newlines=TicketModule.must_preserve_newlines)
                        return descr + format_to(self.env, None, t_context, message)
                    else:
                        return TicketModule.render_timeline_event(context, field, event['event'])
                else:
                    return event['render']('description', context)

            data['my_render_timeline_event'] = my_render_timeline_event

        if template in ['attachment.html']:
            if (data['mode'] in ('new', 'view', 'delete')):
                attachment = data['attachment']
                data['can_delete'] = ('TICKET_ATTACHMENT_DELETE' in
                                      req.perm(attachment.resource))
                if data['attachment'].parent_realm == 'ticket':
                    parent_id = attachment.parent_id
                    ticket = Ticket(self.env, parent_id)
                    data['ticket_type'] = ticket['type']
                    data['ticket_status'] = ticket['status']
                    data['can_delete'] = attachments_can(req,
                                                         ticket,
                                                         data['can_delete'])

                    # New contextual link
                    add_ctxtnav(req,
                                _('Attachments List'),
                                href='%s%s/' % (self.env.base_url,
                                                req.path_info[:req.path_info.rfind('/')]))

                    if data['mode'] == 'new':
                        # Default values
                        data['repo_url'] = self.env.base_url + '/browser'
                        if data["ticket_type"] == 'DOC':
                            data["repo_url"] += util.get_url(ticket["sourceurl"])
                        data['repo_url'] += '?attachment_tid=' + parent_id
                        data['new_attachment_filename'] = None
                        data['rename_checklist'] = False

                        # From repository data
                        data['selected_url'] = req.args.get('selected_url')

                        if data['selected_url']:
                            repos = util.get_repository(self.env,
                                                        data['selected_url'])
                            if repos.reponame:
                                node_url = data['selected_url'][len(repos.reponame) + 1:]
                            else:
                                node_url = data['selected_url']
                            node = repos.get_node(util.get_url(node_url),
                                                  util.get_revision(node_url))
                            if node.isdir:
                                data['repo_url'] = (self.env.base_url +
                                                    '/browser' +
                                                    data['selected_url'] +
                                                    '?attachment_tid=' +
                                                    parent_id)
                                data['selected_url'] = None
                            else:
                                # Renaming proposed if attachment is a checklist
                                if ticket['type'] == 'RF':
                                    attachment_filename = data['selected_url'].split('/')[-1].split('?')[0]
                                    if attachment_filename.startswith('CHKLST_'):
                                        attachment_suffix = attachment_filename.rsplit('.', 1)[-1]
                                        data['new_attachment_filename'] = ticket['summary'].replace('RF_', 'CHKLST_') + '.' + attachment_suffix
                                        attachment_doctype = attachment_filename.rsplit('_', 1)[-1].rsplit('.', 1)[0]
                                        if '_%s_' % attachment_doctype in data['new_attachment_filename']:
                                            data['rename_checklist'] = True
                                # Computing of browser url
                                data['repo_url'] = self.env.base_url + '/browser'
                                url = util.get_url(data['selected_url'])
                                rev = util.get_revision(data['selected_url'])
                                last_path_rev_author = util.get_last_path_rev_author(self.env, url, rev)
                                reponame = last_path_rev_author[0]
                                path = last_path_rev_author[1]
                                rev = last_path_rev_author[2]
                                if reponame:
                                    data['repo_url'] += '/' + reponame
                                data['repo_url'] += path.rsplit('/', 1)[0]  # Parent directory
                                data['repo_url'] += '?attachment_tid=' + parent_id

                        data['attachment_source'] = req.args.get('attachment_source', 'filesystem')

                        if (ticket['type'] == 'MOM' and
                            'MOM' in util.get_prop_values(self.env,
                                                          'ticket_edit.office_suite').keys()):
                            tp_data = form.TicketForm.get_ticket_process_data(self.env, req.authname, ticket)
                            tf = tp_data['ticket_form']
                            data['MOM_with_form'] = util.exist_in_repo(self.env, tf.http_url)

                        if 'add' in req.args:
                            req.perm(attachment.resource).require('ATTACHMENT_CREATE')

                            # Update the attachment resource
                            attachment.description = req.args.get('description', '')
                            attachment.author = req.authname
                            attachment.ipnr = req.remote_addr

                            # Validate attachment
                            for manipulator in AttachmentModule(self.env).manipulators:
                                for field, message in manipulator.validate_attachment(req, attachment):
                                    if field:
                                        raise InvalidAttachment(_(
                                            'Attachment field %(field)s is '
                                            'invalid: %(message)s',
                                            field=field, message=message))
                                    else:
                                        raise InvalidAttachment(_('Invalid attachment: %(message)s',
                                                                  message=message))

                            if data['attachment_source'] == 'repository':

                                # No selection
                                selected_url = req.args.get('selected_url')
                                if not selected_url:
                                    raise TracError(_('No file selected'))

                                # Empty file
                                repos = util.get_repository(self.env, selected_url)
                                if repos.reponame:
                                    node_url = data['selected_url'][len(repos.reponame) + 1:]
                                else:
                                    node_url = data['selected_url']
                                node = repos.get_node(util.get_url(node_url), util.get_revision(node_url))
                                size = node.get_content_length()
                                if size == 0:
                                    raise TracError(_("Can't attach empty file"))

                                # Maximum attachment size (in bytes)
                                max_size = data['max_size']
                                if max_size >= 0 and size > max_size:
                                    raise TracError(_('Maximum attachment size: %(num)s bytes', num=max_size), _('Attachment failed'))

                                # File name
                                filename = 'rename' in req.args and req.args.get('new_attachment_filename') or node.name

                                if req.args.get('replace'):
                                    try:
                                        old_attachment = Attachment(self.env, attachment.resource(id=filename))
                                        if (not attachment.description.strip() and
                                            old_attachment.description):
                                            attachment.description = old_attachment.description
                                        old_attachment.delete()
                                    except TracError:
                                        pass  # don't worry if there's nothing to replace
                                    attachment.filename = None
                                content = node.get_content()
                                attachment.insert(filename, content, size)

                                # Add entry in attachment_custom table if not already in it
                                try:
                                    db = self.env.get_db_cnx()
                                    model.AttachmentCustom(self.env, (attachment.parent_realm, attachment.parent_id, attachment.filename, 'source_url'), db=db)
                                except ResourceNotFound:
                                    prop = model.AttachmentCustom(self.env, db=db)
                                    prop.type = attachment.parent_realm
                                    prop.id = attachment.parent_id
                                    prop.filename = attachment.filename
                                    prop.name = 'source_url'
                                    prop.value = selected_url
                                    prop.insert(db=db)
                                    db.commit()

                            elif data['attachment_source'] == 'generated':
                                host = util.get_hostname(self.env)
                                momtype = ticket['momtype']

                                if momtype == 'CCB':
                                    # the CCB MoM is prepared
                                    mom = form.CCB(self.env, host, req.authname)

                                elif momtype == 'Review':
                                    # the Review MoM for the selected milestone is prepared
                                    mom = form.Review(self.env, host, req.authname)

                                else:
                                    # Type not pre-filled - The MoM will be delivered empty
                                    mom = form.MoM(self.env, host, req.authname)

                                # File name
                                skills = util.get_ticket_skills(self.env, ticket['skill'])
                                filepath = mom.setup(str(ticket.id), skills, ticket['milestonetag'])
                                filename = '%s.docx' % ticket['summary']

                                if req.args.get('replace'):
                                    try:
                                        old_attachment = Attachment(self.env, attachment.resource(id=filename))
                                        if (not attachment.description.strip() and
                                            old_attachment.description):
                                            attachment.description = old_attachment.description
                                        old_attachment.delete()
                                    except TracError:
                                        pass  # don't worry if there's nothing to replace
                                    attachment.filename = None
                                try:
                                    f = open(filepath, 'rb')
                                    try:
                                        attachment.insert(filename,
                                                          f,
                                                          os.path.getsize(filepath))
                                    finally:
                                        f.close()
                                except IOError:
                                    add_warning(req, _("Sorry, could not find the generated MoM. "))

                            req.redirect(get_resource_url(self.env, attachment.resource(id=None), req.href))

                    elif data['mode'] == 'view':

                        attachment_edit = Ticket_UI.get_UI(ticket)(self, ticket).attachment_edit
                        data['supported_suffixes'] = attachment_edit['protocol'].keys()

                        tickets_with_forms = set(('EFR', 'ECR', 'RF', 'PRF'))

                        if 'MOM' in util.get_prop_values(self.env,
                                                         'ticket_edit.office_suite').keys():
                            tickets_with_forms.add('MOM')

                        if ticket['type'] in tickets_with_forms:
                            tp_data = form.TicketForm.get_ticket_process_data(self.env, req.authname, ticket)
                            tf = tp_data['ticket_form']
                            if not util.exist_in_repo(self.env, tf.http_url):
                                tickets_with_forms.remove(ticket['type'])

                        if ticket['type'] in tickets_with_forms:

                            # Setup of working copy for parent ticket in order to be able to modify attachment
                            if not os.path.isdir(tf.path):

                                # Extraction of 'ticket url' folder (in case)
                                unix_cmd_list = ['if [ ! -d "' + tf.program_path + '" ]; then mkdir -p "' + tf.program_path + '"; fi']
                                unix_cmd_list += ['if [ ! -d "' + tf.user_path + '" ]; then mkdir "' + tf.user_path + '"; fi']
                                unix_cmd_list += ['if [ ! -d "' + tf.type_path + '" ]; then ' + tp_data['svn_template_cmd'] % {'subcommand': 'co --depth empty'} + '"' + tf.repo_url + '" "' + tf.type_path + '"; fi']
                                unix_cmd_list += ['if [ ! -d "' + tf.type_subpath + '" ]; then ' + tp_data['svn_template_cmd'] % {'subcommand': 'co --depth empty'} + '"' + tf.repo_suburl + '" "' + tf.type_subpath + '"; fi']

                                # The ticket is checked out
                                unix_cmd_list += [tp_data['svn_template_cmd'] % {'subcommand': 'co --depth files'} + '"' + tf.http_url + '" "' + tf.path + '"']

                                # Effective application of the list of commands
                                util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())

                                # Prepare the edit form according to the current workflow status and ticket ownership
                                tf.prepare_edit_form()

                                # The attachments are checked out
                                unix_cmd_list = [tp_data['svn_template_cmd'] % {'subcommand': 'co --depth files'} + '"' + tp_data['attachment'].http_url + '" "' + tp_data['attachment'].destpath + '"']

                                # Effective application of the list of commands
                                util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())

            elif data['mode'] == 'list':
                if data['attachments']['parent'].realm == 'ticket':
                    parent_id = data['attachments']['parent'].id
                    ticket = Ticket(self.env, parent_id)
                    data['attachments']['can_create'] = attachments_can(
                        req,
                        ticket,
                        data['attachments']['can_create'])

        if template in ['report_view.html',
                        'query.html']:
            data['env'] = self.env
            # Function to get Office Suite used for each ticket type
            data['get_ticket'] = partial(Ticket, self.env)
            data['office_suites'] = util.get_prop_values(self.env, 'ticket_edit.office_suite')
            # 'Available PDF Packages'
            href = '/PDF-printing/%s' % self.trac_env_name
            add_ctxtnav(req, _('Available PDF Packages'), href=href)
            # DOC reports headers customization
            if template == 'query.html' and 'DOC' in data['title']:
                new_labels = {'summary': 'Identifier',
                              'parent': 'Parent MOM',
                              'document': 'Document tag',
                              'blocking': 'Parent ECR(s)'}
                label_keys = new_labels.keys()
                for header in data['headers']:
                    if header['name'] in label_keys:
                        header['label'] = new_labels[header['name']]

        if template == 'report_list.html':
            data['skill_default_value'] = self.config.get('ticket-custom', 'skill.value')

        if template == 'about.html':
            data['env'] = self.env

        return (template, data, content_type)

    # IRequestHandler methods

    def match_request(self, req):
        """ Customization of some requests handling """
        match = re.match(r'/tickets/', req.path_info)
        if match and req.method == 'GET':
            return True
        match = re.match(r'/PDF-(printing|packaging)/', req.path_info)
        if match and req.method == 'GET':
            return True
        match = re.match(r'/clickonce', req.path_info)
        if match and req.method == 'GET':
            req.args = util.parse_query_string(req.query_string)
            return True
        match = re.match(r'/beacon', req.path_info)
        if match and req.method == 'POST':
            req.args = util.parse_query_string(req.read())
            req.args['__FORM_TOKEN'] = req.form_token
            return True
        match = re.match(r'/xhrget', req.path_info)
        if match and req.method == 'GET':
            req.args = util.parse_query_string(req.query_string)
            return True
        match = re.match(r'/xhrpost', req.path_info)
        if match and req.method == 'POST':
            req.args = util.parse_query_string(req.read())
            return True

    def process_request(self, req):
        """ Customization of some requests handling """

        if req.path_info.startswith('/tickets/'):
            # 'View' tickets
            regular_expression = r"^/tickets/([^/]+)/(.+)$"
            m = re.search(regular_expression, req.path_info)
            if m:
                ticket_form_path = '/var/cache/trac/tickets/%s/%s/%s' % (self.trac_env_name,
                                                                         m.group(1),
                                                                         m.group(2))
                # MIME type is left generic because otherwise a suffix
                # is added automatically by the browser if
                # vnd.oasis.opendocument.text is used (the effect being
                # the OOo plugin will not be usable which we
                # don't care about because it is not
                # functionning well at all in Firefox)
                req.send_file(ticket_form_path, 'application/binary')

        elif req.path_info.startswith('/PDF-'):
            # Get PDF package of documents or tickets
            base_dir = '/var/cache/trac/%s/%s' % (req.path_info.split('/')[1], self.trac_env_name)
            package_path = '%s/%s' % (base_dir, os.path.basename(req.path_info))
            suffix = req.path_info.rsplit('.', 1)[-1]
            if suffix == 'zip':
                mimetype = 'application/zip'
            elif suffix == 'idx':
                mimetype = 'application/txt'
            else:
                mimetype = None
            req.send_file(package_path, mimetype)

        elif req.path_info.startswith('/clickonce'):
            # Start PDF generation
            ticket_id = req.args.get('ticket_id')
            ticket = Ticket(self.env, ticket_id)
            template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
            doc = template_cls(self.env, self.trac_env_name, req.authname, ticket)
            url = doc.get_publish_url(ticket['sourcefile'], ticket['pdffile'])
            xhrurl = self.env.base_url + '/xhrpost'
            url += '&xhrurl=' + xhrurl + '&authname=' + req.authname + '&ticketid=' + ticket_id
            if ticket['type'] == 'DOC':
                url += '&status=' + req.args.get('status') + '&attachments=' + req.args.get('attachments') + '&charts=' + req.args.get('charts') + '&markups=' + req.args.get('markups') + '&automation=' + req.args.get('automation')
            else:
                url += '&charts=' + req.args.get('charts') + '&markups=' + req.args.get('markups') + '&automation=' + req.args.get('automation')
            req.redirect(url)

        elif req.path_info.startswith('/beacon'):
            # Cancel PDF generation wait
            ticket_id = req.args.get('ticket_id')
            ticket = Ticket(self.env, ticket_id)
            template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
            doc = template_cls(self.env, self.trac_env_name, req.authname, ticket)
            doc.create_flag(ticket['sourcefile'], 'ko')
            req.send('PDF generation cancelled because the user left the page', 'text/plain')

        elif req.path_info.startswith('/xhrget'):
            db = self.env.get_db_cnx()
            cursor = db.cursor()
            field_name = req.args.get('field')

            if field_name == 'configurationitem':
                branch_segregation_activated = True if self.env.config.get('artusplugin', 'branch_segregation_activated') == 'True' else False
                if branch_segregation_activated:
                    branch_segregation_first_branch = self.env.config.get('artusplugin', 'branch_segregation_first_branch', 'B1')
                else:
                    branch_segregation_first_branch = None

                def unicity(tags, segregation):
                    seen = set()
                    for tg in tags:
                        if segregation:
                            branch = model.NamingRule.get_branch_from_tag(self.env, tg.name)
                            if (tg.tracked_item, branch) not in seen and not seen.add((tg.tracked_item, branch)):
                                yield tg
                        else:
                            if tg.tracked_item not in seen and not seen.add(tg.tracked_item):
                                yield tg

                class ConfigurationItem(object):
                    def __init__(self, env, ci_data, selected):
                        if isinstance(ci_data, model.Tag):
                            self.ci_name = ci_data.tracked_item
                            self.ci_selected = selected
                            self.branch_name = model.NamingRule.get_branch_from_tag(env, ci_data.name)
                        elif type(ci_data) is tuple:
                            self.ci_name = ci_data[0]
                            self.ci_selected = selected
                            self.branch_name = util.get_branch(ci_data[1])
                        else:
                            raise TracError(_("ConfigurationItem: incorrect parameter type"))
                        if self.branch_name == 'trunk':
                            self.branch_number = 0
                        elif self.branch_name.startswith('B'):
                            self.branch_number = int(self.branch_name[1:])
                        else:
                            self.branch_number = -1

                # Document CI list
                skill = req.args.get('skill')
                document_tags = list(unicity(
                    [v for v in model.Tag.select(self.env, ['component=0', 'version_type=0'],
                                                 db=db, tag_type='version_tags')
                    if not v.tracked_item.startswith('ECM_%s_' % self.program_name)
                    and util.get_skill(self.env, v.tracked_item, self.program_name) == skill
                    and util.node_is_dir(self.env, util.get_url(v.source_url))],
                    branch_segregation_activated))

                # Skill and name filter
                document_cis = []
                ci_req = None
                url_req = None
                branch_req = None

                if 'configurationitem' in req.args and 'sourceurl' in req.args:
                    ci_req = req.args.get('configurationitem')
                    url_req = util.unicode_unquote_plus(req.args.get('sourceurl'))
                    regexp = '^%s\_%s\_' % (self.program_name, skill)
                    if re.search(regexp, ci_req) and util.node_is_dir(self.env, util.get_url(url_req)):
                        document_cis += [ConfigurationItem(self.env, (ci_req, url_req), True)]
                        branch_req = document_cis[0].branch_name

                document_cis += [ConfigurationItem(self.env, d_tg, False) for d_tg in document_tags
                                 if d_tg.tracked_item != ci_req or util.get_url(d_tg.source_url) != util.get_url(url_req)]

                grouped_configurationitems = util.group_by(document_cis, ('branch_number', True))

                document_cis_by_branch = OrderedDict()
                for branch_group in grouped_configurationitems:
                    if branch_group[-1].branch_name == 'trunk':
                        document_cis_by_branch[branch_group[-1].branch_name] = [(ci.ci_name, ci.ci_selected) for ci in branch_group]
                for branch_group in grouped_configurationitems:
                    if branch_group[-1].branch_name != 'trunk' and branch_group[-1].branch_name != '?':
                        if not branch_segregation_activated or branch_group[-1].branch_number >= int(branch_segregation_first_branch[1:]):
                            document_cis_by_branch[branch_group[-1].branch_name] = [(ci.ci_name, ci.ci_selected) for ci in branch_group]
                for branch_group in grouped_configurationitems:
                    if branch_group[-1].branch_name == '?':
                        document_cis_by_branch[branch_group[-1].branch_name] = [(ci.ci_name, ci.ci_selected) for ci in branch_group]
                if branch_req:
                    document_cis_by_branch[branch_req].sort()

                field_value = json.dumps(document_cis_by_branch)

            elif field_name == 'milestone':
                milestones = [m.name for m in Milestone.select(self.env,
                              include_completed=False, db=db) if m.name != 'Dummy']
                if milestones:
                    # Filter
                    skill = req.args.get('skill')
                    milestones = util.get_filtered_items(
                        self.env,
                        milestones,
                        util.get_milestone_skills(self.env, skill))
                milestones.insert(0, '')
                field_value = json.dumps([(milestone, milestone == req.args.get('milestone')) for milestone in milestones])

            elif field_name == 'milestonetag':
                skill = req.args.get('skill')
                momtype = req.args.get('momtype')
                if momtype == 'CCB':
                    condition = 'tracked_item="%s" AND status="Prepared"' % skill
                elif momtype == 'Review':
                    condition = 'tracked_item="%s" AND status="Reviewed"' % skill
                else:
                    condition = ''
                if condition:
                    db = self.env.get_db_cnx()
                    # Accepted milestones shall be filtered out
                    accepted_milestones = [v.tagged_item
                                           for v in model.Tag.select(
                                               self.env,
                                               ['status="Accepted"',
                                                'tag_url IS NOT NULL'],
                                               db=db,
                                               tag_type='milestone_tags')]
                    milestone_tags = [v
                                      for v in model.Tag.select(
                                          self.env,
                                          [condition],
                                          ordering_term='tagged_item,name',
                                          db=db,
                                          tag_type='milestone_tags')
                                      if v.tagged_item not in accepted_milestones]
                    # Miletone tags already referenced by a CCB MOM ticket shall be filtered out
                    milestone_tags = [v for v in milestone_tags if not util.get_mom_tktid(self.env, v)]
                    grouped_milestone_tags = util.group_by(milestone_tags, ('tagged_item', False))
                else:
                    grouped_milestone_tags = []
                grouped_options = OrderedDict()
                for tagged_group in grouped_milestone_tags:
                    grouped_options[tagged_group[-1].tagged_item] = [tag.name for tag in tagged_group]
                field_value = json.dumps(grouped_options)

            elif field_name == 'fromversion':
                configurationitem = req.args.get('configurationitem');
                branch = req.args.get('branch');
                changetype = req.args.get('changetype')
                fromversion = req.args.get('fromversion')
                instr_pattern = '/trunk/' if branch == 'trunk' else '/branches/%s/' % branch

                if configurationitem == 'null':
                    fromversions = []

                elif changetype == 'Edition':
                    sql = ("SELECT max(u.tagged_item) FROM "
                           "( "
                           "SELECT DISTINCT tagged_item FROM tag "
                           "WHERE tracked_item='%s' "
                           "AND instr(source_url, '%s')=1 "
                           "AND edition=( "
                           "SELECT max(edition) FROM tag "
                           "WHERE tracked_item='%s') "
                           "UNION "
                           "SELECT '%s'||tc1.value AS tagged_item "
                           "FROM ticket_custom tc1,ticket_custom tc2,ticket_custom tc3 "
                           "WHERE tc1.ticket = tc2.ticket "
                           "AND tc1.name='versionsuffix' "
                           "AND tc2.name='configurationitem' "
                           "AND tc2.value='%s' "
                           "AND tc1.ticket = tc3.ticket "
                           "AND tc3.name='sourceurl' "
                           "AND instr(tc3.value, '%s')=1 "
                           ") u "
                           "ORDER BY u.tagged_item DESC"
                           % (configurationitem, instr_pattern,
                              configurationitem, configurationitem,
                              configurationitem, instr_pattern)
                           )
                    cursor.execute(sql)
                    row = cursor.fetchone()
                    if row and row[0]:
                        fromversions = [(row[0], True)]
                    else:
                        if branch == 'trunk':
                            fromversions = [('New Document', True)]
                        else:
                            fromversions = [('New Branch Document (%s)' % branch, True)]

                elif changetype == 'Revision':
                    # edition converted to string in order to achieve unicity
                    sql = ("SELECT DISTINCT u.tagged_item, ''||u.edition FROM "
                           "( "
                           "SELECT DISTINCT tagged_item,edition,revision FROM tag "
                           "WHERE tracked_item='%s' "
                           "AND instr(source_url, '%s')=1 "
                           "UNION "
                           "SELECT DISTINCT '%s'||tc1.value AS tagged_item, "
                           "rtrim(round(ltrim(tc1.value,'_')),'.0') AS edition, "
                           "substr(ltrim(tc1.value,'_'), "
                           "length(round(ltrim(tc1.value,'_')))) AS revision "
                           "FROM ticket_custom tc1,ticket_custom tc2,ticket_custom tc3  "
                           "WHERE tc1.ticket = tc2.ticket "
                           "AND tc1.name='versionsuffix' "
                           "AND tc2.name='configurationitem' "
                           "AND tc2.value='%s' "
                           "AND tc1.ticket = tc3.ticket "
                           "AND tc3.name='sourceurl' "
                           "AND instr(tc3.value, '%s')=1 "
                           ") u "
                           "ORDER BY u.edition ASC, u.revision DESC"
                           % (configurationitem, instr_pattern,
                              configurationitem,
                              configurationitem, instr_pattern)
                           )
                    cursor.execute(sql)
                    seen = set()
                    fromversions = [(row[0], row[0] == fromversion) for row in cursor
                                    if row[1] not in seen and
                                    not seen.add(row[1])]

                elif changetype == 'Status':
                    sql = ("SELECT DISTINCT u.tagged_item FROM "
                           "( "
                           "SELECT DISTINCT tagged_item FROM tag "
                           "WHERE tracked_item='%s' "
                           "AND instr(source_url, '%s')=1 "
                           "and tagged_item||'.Released' NOT IN "
                           "(SELECT tag2.name FROM tag AS tag2 "
                           "WHERE tag2.tagged_item=tag.tagged_item "
                           "AND instr(tag2.source_url, '%s')=1) "
                           "EXCEPT "
                           "SELECT '%s'||tc1.value AS tagged_item "
                           "FROM ticket_custom tc1,ticket_custom tc2,ticket_custom tc3 "
                           "WHERE tc1.ticket = tc2.ticket "
                           "AND tc1.name='versionsuffix' "
                           "AND tc2.name='configurationitem' "
                           "AND tc2.value='%s' "
                           "AND tc1.ticket = tc3.ticket "
                           "AND tc3.name='sourceurl' "
                           "AND instr(tc3.value, '%s')=1 "
                           ") u "
                           "ORDER BY u.tagged_item ASC"
                           % (configurationitem, instr_pattern,
                              instr_pattern, configurationitem,
                              configurationitem, instr_pattern)
                           )
                    cursor.execute(sql)
                    fromversions = [(row[0], row[0] == fromversion) for row in cursor]
                    if fromversions and (not fromversion or fromversion == 'null'):
                        fromversions[-1] = (fromversions[-1][0], True)

                else:
                    # To remove ? Ticket DOC inapplicable to EXT skill
                    sql = ("SELECT DISTINCT tagged_item FROM tag "
                           "WHERE tracked_item='%s' "
                           "ORDER BY tagged_item ASC"
                           % configurationitem)
                    cursor.execute(sql)
                    fromversions = [(row[0], row[0] == fromversion) for row in cursor]
                    if fromversions and (not fromversion or fromversion == 'null'):
                        fromversions[-1] = (fromversions[-1][0], True)

                field_value = json.dumps(fromversions)

            elif field_name == 'versionsuffix':
                changetype = req.args.get('changetype')
                fromversion = req.args.get('fromversion')

                regexp = '.*?_(\d+)\.(\d+)\.(\d+)'
                match = re.search(regexp, fromversion)
                if match:
                    standard = match.group(1)
                    edition = match.group(2)
                    revision = match.group(3)
                    if changetype == 'Edition':
                        versionsuffix = "_%s.%s.0" % (standard, int(edition) + 1)
                    elif changetype == 'Revision':
                        versionsuffix = "_%s.%s.%s" % (standard, edition, int(revision) + 1)
                    elif changetype == 'Status':
                        versionsuffix = "_%s.%s.%s" % (standard, edition, revision)
                else:
                    regexp = '.*?_(\d+)\.(\d+)'
                    match = re.search(regexp, fromversion)
                    if match:
                        edition = match.group(1)
                        revision = match.group(2)
                        if changetype == 'Edition':
                            versionsuffix = "_%s.0" % (int(edition) + 1)
                        elif changetype == 'Revision':
                            versionsuffix = "_%s.%s" % (edition, int(revision) + 1)
                        elif changetype == 'Status':
                            versionsuffix = "_%s.%s" % (edition, revision)

                field_value = json.dumps(versionsuffix)

            elif field_name == 'fromecm':
                fromecms = [('New Technical Note', False)]
                sql = ("SELECT summary FROM "
                       "ticket,ticket_custom tc "
                       "WHERE id=ticket "
                       "AND tc.name='ecmtype' "
                       "AND tc.value='Technical Note'")
                cursor.execute(sql)
                rows = cursor.fetchall() or []
                if rows:
                    regexp = "^(ECM_%s_\d{3})_v\d+" % self.program_name
                    ecms = [(row[0], re.match(regexp, row[0])) for row in rows if re.search(regexp, row[0])]
                    ecms = sorted([(ecm, match.group(1)) for (ecm, match) in ecms], key=lambda x:x[0], reverse=True)
                    seen = set()
                    ecms = sorted([ecm for (ecm, group) in ecms if group not in seen and not seen.add(group)])
                    fromecms += [(ecm, False) for ecm in ecms]
                fromecm = req.args.get('fromecm')
                if fromecm:
                    fromecms.append((fromecm, True))
                field_value = json.dumps(fromecms)

            elif field_name == 'fromfee':
                fromfees = [('New Evolution Sheet', False)]
                sql = ("SELECT summary FROM "
                       "ticket "
                       "WHERE type='FEE'")
                cursor.execute(sql)
                rows = cursor.fetchall() or []
                if rows:
                    regexp = "^(FEE_%s_\d{5}-\d{2})_v\d+" % self.program_name
                    fees = [(row[0], re.match(regexp, row[0])) for row in rows if re.search(regexp, row[0])]
                    fees = sorted([(fee, match.group(1)) for (fee, match) in fees], key=lambda x:x[0], reverse=True)
                    seen = set()
                    fees = sorted([fee for (fee, group) in fees if group not in seen and not seen.add(group)])
                    fromfees += [(fee, False) for fee in fees]
                fromfee = req.args.get('fromfee')
                if fromfee:
                    fromfees.append((fromfee, True))
                field_value = json.dumps(fromfees)

            elif field_name == 'evolref':
                fromfee = req.args.get('fromfee')
                if fromfee == 'New Evolution Sheet':
                    # Get list of Evolution References from SQL Server View
                    evolrefs = util.SqlServerView().get_evolrefs()
                    # Filter out Evolution References alreaded created
                    sql = ("SELECT summary FROM "
                        "ticket "
                        "WHERE type='FEE'")
                    cursor.execute(sql)
                    rows = cursor.fetchall() or []
                    if rows:
                        regexp = "^FEE_%s_(\d{5}-\d{2})_v\d+" % self.program_name
                        refs = {re.match(regexp, row[0]).group(1) for row in rows if re.search(regexp, row[0])}
                        evolrefs = [ref for ref in evolrefs if ref not in refs]
                else:
                    evolrefs = []
                field_value = json.dumps(evolrefs)

            elif field_name == 'customer':
                evolref = req.args.get('evolref')
                # Get list of Customer(s) from SQL Server View
                customers = util.SqlServerView().get_customers(evolref)
                field_value = json.dumps(customers)

            elif field_name == 'program':
                evolref = req.args.get('evolref')
                customer = req.args.get('customer')
                # Get list of Programs(s) from SQL Server View
                programs = util.SqlServerView().get_programs(evolref, customer)
                field_value = json.dumps(programs)

            elif field_name == 'application':
                evolref = req.args.get('evolref')
                customer = req.args.get('customer')
                program = req.args.get('program')
                # Get list of Application(s) from SQL Server View
                applications = util.SqlServerView().get_applications(evolref, customer, program)
                field_value = json.dumps(applications)

            elif field_name == 'itemsdisplay':
                evolref = req.args.get('evolref')
                customer = req.args.get('customer')
                program = req.args.get('program')
                application = req.args.get('application')
                # Get list of Items(s) from SQL Server View
                wiki_to_html = partial(format_to_html, self.env)
                context = Context.from_request(req, Ticket(self.env).resource)
                wiki = "||=  '''Article'''  =||=  '''PN produit fini'''  =||=  '''Amendement'''  =||\n"
                for row in util.SqlServerView().get_items(evolref, customer, program, application):
                    wiki += "||=  %s  =||=  %s  =||=  %s  =||\n" % (row[0], row[1], row[2])
                html = wiki_to_html(context, wiki)
                field_value = json.dumps(html)

            elif field_name == 'keywords':
                summary = req.args.get('summary')
                sql = ("SELECT keywords FROM "
                       "ticket "
                       "WHERE summary='%s'"
                       % summary)
                cursor.execute(sql)
                row = cursor.fetchone()
                if row:
                    keywords = row[0]
                else:
                    keywords = ''
                field_value = json.dumps(keywords)

            elif field_name == 'distribution':
                ticket_id = req.args.get('ticket_id')
                ticket = Ticket(self.env, ticket_id)
                if ticket['toname'] or ticket['toemail'] or ticket['tophone']:
                    toname = ticket['toname']
                    toemail = ticket['toemail']
                    tophone = ticket['tophone']
                else:
                    from_value = ticket['fromecm'] if ticket['type'] == 'ECM' else ticket['fromfee']
                    sql = ("SELECT id FROM "
                           "ticket "
                           "WHERE summary='%s'"
                           % from_value)
                    cursor.execute(sql)
                    row = cursor.fetchone()
                    if row:
                        from_id = row[0]
                        from_tkt = Ticket(self.env, from_id)
                        toname = from_tkt['toname']
                        toemail = from_tkt['toemail']
                        tophone = from_tkt['tophone']
                    else:
                        toname = ''
                        toemail = ''
                        tophone = ''
                field_value = json.dumps(dict([('toname', toname),
                                               ('toemail', toemail),
                                               ('tophone', tophone)]))

            elif field_name == "sourcetype":
                # Default value
                source_types = util.get_prop_values(self.env, "source_types")
                st_default = source_types.keys()[0]
                ci_name = req.args.get("configurationitem")
                if ci_name != "null":
                    cursor.execute(
                        "SELECT sourcetype FROM document WHERE name='%s'" % ci_name
                    )
                    row = cursor.fetchone()
                    if row and row[0]:
                        # Filtered list
                        st_values = [(row[0], True, DOC_UI.sourcetype_tip(self.env)[row[0]])]
                    else:
                        # default value set from CI
                        for st in source_types:
                            regexp = source_types[st].split('||')[4].strip()
                            if regexp and pcre.search(regexp, ci_name):
                                st_default = st
                                break
                        st_values = [
                            (
                                st_value,
                                st_value == st_default,
                                DOC_UI.sourcetype_tip(self.env)[st_value],
                            )
                            for st_value in DOC_UI.sourcetype_tip(self.env).keys()[1:]
                        ]
                grouped_options = OrderedDict()
                for (st_value, selected, tip) in st_values:
                    key = st_value.split(':')[0]
                    option = st_value.split(':')[1]
                    grouped_options.setdefault(key, []).append((option, selected, tip))

                field_value = json.dumps(grouped_options)

            elif field_name == "pdfsigned":
                # Default value
                source_types = util.get_prop_values(self.env, "source_types")
                sourcetype = req.args.get("sourcetype")
                if sourcetype != "null":
                    if sourcetype in source_types:
                        ps_value = source_types[sourcetype].split('||')[1].strip()
                    else:
                        ps_value = "true"
                else:
                    ps_value = "true"
                # Setup value
                ci_name = req.args.get("configurationitem")
                if ci_name != "null":
                    cursor.execute(
                        "SELECT pdfsigned FROM document " "WHERE name='%s'" % ci_name
                    )
                    row = cursor.fetchone()
                    if row:
                        ps_value = (
                            "false" if row[0] is not None and row[0] == 0 else "true"
                        )
                field_value = json.dumps(ps_value)

            elif field_name == "independence":
                # Default value
                source_types = util.get_prop_values(self.env, "source_types")
                sourcetype = req.args.get("sourcetype")
                if sourcetype != "null":
                    if sourcetype in source_types:
                        ip_value = source_types[sourcetype].split('||')[2].strip()
                    else:
                        ip_value = "true"
                else:
                    ip_value = "true"
                # Setup value
                ci_name = req.args.get("configurationitem")
                if ci_name != "null":
                    cursor.execute(
                        "SELECT independence FROM document " "WHERE name='%s'" % ci_name
                    )
                    row = cursor.fetchone()
                    if row:
                        ip_value = (
                            "false" if row[0] is not None and row[0] == 0 else "true"
                        )
                field_value = json.dumps(ip_value)

            elif field_name == 'controlcategory':
                # Default value
                source_types = util.get_prop_values(self.env, "source_types")
                sourcetype = req.args.get("sourcetype")
                if sourcetype != "null":
                    cc_value = source_types[sourcetype].split('||')[3].strip()
                else:
                    cc_value = "CC2/HC2"
                # List of values
                cc_options = self.env.config.get('ticket-custom',
                                                 'controlcategory.options')
                options = [option.strip() for option in cc_options.split('|')] if cc_options else []
                cc_values = [(option,
                              option == cc_value,
                              DOC_UI.controlcategory_tip[option])
                             for option in options]
                # Force document selected value
                ci_name = req.args.get('configurationitem')
                if ci_name != 'null' and cc_values:
                    cursor.execute("SELECT controlcategory FROM document "
                                "WHERE name='%s'" % ci_name)
                    row = cursor.fetchone()
                    if row:
                        index = row[0] if row[0] is not None else 0
                        cc_values = [cc_values[index]]
                field_value = json.dumps(cc_values)

            elif field_name == 'submittedfor':
                # Default value
                sf_value = self.env.config.get('ticket-custom',
                                                 'submittedfor.value', 'Approval')
                # List of values
                sf_options = self.env.config.get('ticket-custom',
                                                 'submittedfor.options')
                options = [option.strip() for option in sf_options.split('|')] if sf_options else []
                sf_values = [(option,
                              option == sf_value,
                              DOC_UI.submittedfor_tip[option])
                             for option in options]
                ci_name = req.args.get('configurationitem')
                if ci_name != 'null':
                    cursor.execute("SELECT submittedfor FROM document "
                                   "WHERE name='%s'" % ci_name)
                    row = cursor.fetchone()
                    if row:
                        index = row[0] if row[0] is not None else 0
                        sf_values = [sf_values[index]]
                field_value = json.dumps(sf_values)

            elif field_name == 'sourceurl':
                ticket_id = req.args.get('ticket_id')
                ticket = Ticket(self.env, ticket_id)
                field_value = json.dumps(dict([('sourceurl', ticket['sourceurl']),
                                               ('view_time', to_utimestamp(ticket['changetime']))]))

            elif field_name in ('src_wc_status', 'pdf_wc_status'):
                ticket_id = req.args.get('ticket_id')
                ticket = Ticket(self.env, ticket_id)
                if ticket['status'] == '01-assigned_for_edition':
                    last_rev_repo = util.get_last_path_rev_author(
                        self.env, util.get_url(ticket['sourceurl']))[2]
                    if last_rev_repo == util.get_revision(ticket['sourceurl']):
                        template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
                        with template_cls(self.env,
                                          self.trac_env_name,
                                          req.authname,
                                          ticket) as doc:
                            docfile = (ticket['sourcefile']
                                       if field_name == 'src_wc_status'
                                       else ticket['pdffile'])
                            wc_status = doc.status(docfile, 'wc-status')
                            field_value = json.dumps(wc_status['change_status'])
                    else:
                        field_value = json.dumps('update required')
                else:
                    field_value = json.dumps('ticket not in edition')

            elif field_name in ('src_locker', 'pdf_locker'):
                ticket_id = req.args.get('ticket_id')
                ticket = Ticket(self.env, ticket_id)
                template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
                with template_cls(self.env,
                                  self.trac_env_name,
                                  req.authname,
                                  ticket) as doc:
                    docfile = (ticket['sourcefile']
                               if field_name == 'src_locker'
                               else ticket['pdffile'])
                    repos_status = doc.status(docfile, 'repos-status')
                    if repos_status['lock_agent'] == 'trac':
                        locker = 'Locked by %s with ticket #%s' % (repos_status['lock_client'], repos_status['lock_ticket'])
                    elif repos_status['lock_agent'] is None:
                        locker = 'Not locked'
                    else:
                        locker = 'Locked outside of trac by %s' % repos_status['lock_agent']

                    field_value = json.dumps(locker)

            elif field_name == 'sourcefile_url':
                sourcefile = req.args.get('sourcefile')
                if sourcefile == 'N/A':
                    href = ''
                    comment = 'N/A'
                else:
                    ticket_id = req.args.get('ticket_id')
                    ticket = Ticket(self.env, ticket_id)
                    template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
                    with template_cls(self.env,
                                      self.trac_env_name,
                                      req.authname,
                                      ticket) as doc:
                        repo_url = ticket['sourceurl']
                        url = util.get_url(repo_url)
                        revision = util.get_revision(repo_url)
                        if doc.exist_in_repo(sourcefile, revision):
                            url = util.get_tracbrowserurl(self.env, url)
                            href = '%s/%s?format=raw&rev=%s' % (url, sourcefile, revision)
                            comment = 'document folder'
                        else:
                            template_realfilename = os.path.basename(os.path.realpath(doc.template))
                            shared_templates_dir = Chrome(self.env).get_templates_dirs()[1]
                            if shared_templates_dir in doc.template:
                                url = '/templates/share/%s' % template_realfilename
                            else:
                                url = '/templates/env/%s' % template_realfilename
                            suffixes = util.get_prop_values(self.env, 'source_files_suffix')
                            suffix = template_realfilename.split('.')[-1]
                            if suffixes[suffix] == 'MS Office':
                                clickonce_app_url = self.env.config.get('artusplugin', 'clickonce_app_url_MS_Office')
                            else:
                                clickonce_app_url = self.env.config.get('artusplugin', 'clickonce_app_url_OpenOffice')
                            scheme = self.env.config.get('artusplugin', 'scheme')
                            webdav_protocol = util.get_prop_values(self.env, 'webdav_protocol')[suffix]
                            if scheme == 'https':
                                webdav_protocol += 's'
                            href = '%s?action=%s&mode=webdav&url=%s://%s%s' % (
                                clickonce_app_url,
                                'view',
                                webdav_protocol,
                                util.get_hostname(self.env),
                                url)
                            comment = 'template folder'
                field_value = json.dumps(dict([('href', href), ('comment', comment)]))

            elif field_name == 'pdffile_url':
                pdffile = req.args.get('pdffile')
                if pdffile == 'N/A':
                    href = ''
                    comment = 'N/A'
                else:
                    ticket_id = req.args.get('ticket_id')
                    ticket = Ticket(self.env, ticket_id)
                    template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
                    with template_cls(self.env,
                                      self.trac_env_name,
                                      req.authname,
                                      ticket) as doc:
                        repo_url = ticket['sourceurl']
                        url = util.get_url(repo_url)
                        revision = util.get_revision(repo_url)
                        if doc.exist_in_repo(pdffile, revision):
                            repos = util.get_repository(self.env, url)
                            node = repos.get_node('%s/%s' % (url, pdffile), revision)
                            if node.content_length != 0:
                                url = util.get_tracbrowserurl(self.env, url)
                                href = '%s/%s?format=raw&rev=%s' % (url, pdffile, revision)
                                comment = 'document folder'
                            else:
                                href = ''
                                comment = 'empty'
                        else:
                            href = ''
                            comment = 'does not exist'
                field_value = json.dumps(dict([('href', href), ('comment', comment)]))

            elif field_name in ('sourcefile_exists', 'pdffile_exists'):
                docfile = req.args.get('docfile')
                if docfile == 'N/A':
                    exist = False
                else:
                    ticket_id = req.args.get('ticket_id')
                    ticket = Ticket(self.env, ticket_id)
                    template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
                    with template_cls(self.env,
                                      self.trac_env_name,
                                      req.authname,
                                      ticket) as doc:
                        exist = doc.exist_in_wc(docfile)
                field_value = json.dumps(exist)

            elif field_name in ('src_repos_status', 'pdf_repos_status'):
                ticket_id = req.args.get('ticket_id')
                ticket = Ticket(self.env, ticket_id)
                if ticket['status'] != 'closed':
                    last_rev_repo = util.get_last_path_rev_author(
                        self.env, util.get_url(ticket['sourceurl']))[2]
                    if last_rev_repo == util.get_revision(ticket['sourceurl']):
                        template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
                        with template_cls(self.env,
                                          self.trac_env_name,
                                          req.authname,
                                          ticket) as doc:
                            docfile = (ticket['sourcefile']
                                       if field_name == 'src_repos_status'
                                       else ticket['pdffile'])
                            repos_status = doc.status(docfile, 'repos-status')
                            if repos_status['lock_agent'] == 'trac':
                                if repos_status['lock_client'] == req.authname:
                                    locker = ''
                                else:
                                    locker = repos_status['lock_client']
                            elif repos_status['lock_agent'] is None:
                                locker = ''
                            else:
                                locker = repos_status['lock_agent']
                            field_value = json.dumps(locker)
                    else:
                        field_value = json.dumps('update required')
                else:
                    field_value = json.dumps('ticket closed')

            elif field_name == 'workflow':
                ticket_id = req.args.get('ticket_id')
                ticket = Ticket(self.env, ticket_id)
                workflow = self.action_controllers[0]
                ticket_wf = workflow.get_ticket_wf(ticket)(workflow, req, ticket)
                activities = ticket_wf.get_activities(**req.args)
                allowed_actions = ticket_wf.get_allowed_actions(**req.args)
                field_value = json.dumps(dict([('activities', activities),
                                               ('allowed_actions', allowed_actions)]))

            elif field_name == 'lock_unlock_description':
                ticket_id = req.args.get('ticket_id')
                ticket = Ticket(self.env, ticket_id)
                src_file = req.args.get('src_file')
                pdf_file = req.args.get('pdf_file')
                template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
                lock_description = template_cls.lock_description(src_file)
                unlock_description = template_cls.unlock_description(src_file, pdf_file)
                field_value = json.dumps(dict([('lock_description', lock_description),
                                               ('unlock_description', unlock_description)]))

            elif field_name == 'mom_lock_status':
                ticket_id = req.args.get('ticket_id')
                ticket = Ticket(self.env, ticket_id)
                tp_data = form.TicketForm.get_ticket_process_data(self.env, req.authname, ticket)
                tf = tp_data['ticket_form']
                field_value = json.dumps(tf.lock_status())

            elif field_name == 'change_history':
                ticket_id = req.args.get('ticket_id')
                ticket = Ticket(self.env, ticket_id)
                ticket_module = TicketModule(self.env)
                data = ticket_module._prepare_data(req, ticket)
                data['start_time'] = ticket['changetime']
                data['has_edit_comment'] = 'TICKET_EDIT_COMMENT' in req.perm(ticket.resource)
                data['can_append'] = 'TICKET_APPEND' in req.perm(ticket.resource)
                data.update({'comment': req.args.get('comment'),
                             'cnum_edit': req.args.get('cnum_edit'),
                             'edited_comment': req.args.get('edited_comment'),
                             'cnum_hist': req.args.get('cnum_hist'),
                             'cversion': req.args.get('cversion')})
                field_changes = {}
                ticket_module._insert_ticket_data(
                    req, ticket, data, get_reporter_id(req, 'author'), field_changes)
                self.change_history_filter(ticket, data)
                return 'ticket_changes.html', data, None

            elif field_name == 'sourcefile':
                ticket_id = req.args.get('ticket_id')
                ticket = Ticket(self.env, ticket_id)
                if ticket['status'] == '01-assigned_for_edition':
                    template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
                    with template_cls(self.env, self.trac_env_name,
                                      req.authname, ticket) as doc:
                        if (ticket['sourcefile'] and
                            ticket['sourcefile'].endswith('.docm') and
                            doc.exist_flag(ticket['sourcefile'], 'go')):
                            doc.remove_flag(ticket['sourcefile'], 'go')
                            go = True
                        else:
                            go = False
                else:
                    go = False
                field_value = json.dumps(go)

            elif field_name == 'ecr_mom_report_url':
                milestone = req.args.get('milestone')
                skill = req.args.get('skill')
                if not skill:
                    ticket_id = req.args.get('ticket_id')
                    if not ticket_id:
                        raise TracError(_("xhrget unknown ticket id"))
                    else:
                        ticket = Ticket(self.env, ticket_id)
                        skill = ticket['skill']
                ecr_report_url = ''
                ECR_report = self.env.config.get('artusplugin', 'ECR_report')
                db = self.env.get_db_cnx()
                cursor = db.cursor()
                cursor.execute("SELECT query FROM report "
                               "WHERE id=%s" % ECR_report)
                row = cursor.fetchone()
                if row:
                    ecr_query_string = re.sub(r'skill=.*?\n', 'skill=%s\n' % skill, row[0])
                    ecr_query_string = (ecr_query_string.replace('query:', '') +
                                        '&report=%s' % ECR_report)
                    ecr_report_url = req.href('query') + ecr_query_string
                mom_report_url = ''
                MOM_report = self.env.config.get('artusplugin', 'MOM_report')
                mom_report_url = req.href.report(MOM_report,
                                                 SKILL=skill,
                                                 MILESTONE=milestone,
                                                 ACTIVITY='Configuration Management')
                field_value = json.dumps(dict([('ecr_report_url', ecr_report_url),
                                               ('mom_report_url', mom_report_url),
                                               ('skill', skill)]))

            else:
                raise TracError(_("xhrget unknown field name"))

            req.send(field_value.encode("utf-8"))

        elif req.path_info.startswith('/xhrpost'):
            ticket_id = req.args.get('ticket_id')
            ticket = Ticket(self.env, ticket_id)
            action = req.args.get('action')

            if action in ('wait_unlock', 'prepare_src', 'commit', 'schedule_lock', 'unschedule_lock', 'lock', 'unlock'):

                if ticket['status'] == '01-assigned_for_edition':

                    if ((ticket['sourcefile'] and ticket['sourcefile'] != 'N/A') or
                        (ticket['pdffile'] and ticket['pdffile'] != 'N/A')):

                        if action == 'wait_unlock':

                            template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
                            with template_cls(self.env, self.trac_env_name,
                                              req.authname, ticket) as doc:
                                if (ticket['sourcefile'] and ticket['sourcefile'] != 'N/A'):
                                    doc.remove_flag(ticket['sourcefile'], "ok")
                                    doc.remove_flag(ticket['sourcefile'], "ko")
                                if (ticket['pdffile'] and ticket['pdffile'] != 'N/A'):
                                    doc.remove_flag(ticket['pdffile'], "ok")

                            def wait_unlock(docfile):
                                # Wait until PDF file is generated or generation is cancelled
                                wait = True
                                while wait:
                                    sleep(1)
                                    template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
                                    with template_cls(self.env, self.trac_env_name,
                                                      req.authname, ticket) as doc:
                                        # Abort wait
                                        if doc.exist_flag(ticket['sourcefile'], 'ko'):
                                            doc.remove_flag(ticket['sourcefile'], 'ko')
                                            raise TracError(_("Publication cancelled."))
                                        # End wait
                                        if doc.exist_flag(docfile, 'ok'):
                                            doc.remove_flag(docfile, 'ok')
                                            wait = False

                            if ticket['pdffile'] and ticket['pdffile'] != 'N/A':
                                wait_unlock(ticket['pdffile'])
                            elif ticket['sourcefile'] and ticket['sourcefile'] != 'N/A':
                                wait_unlock(ticket['sourcefile'])

                        elif action == 'prepare_src':

                            # Anonymous request from ProtocolHandler
                            # so we have to supply the authname
                            # which is required to access the proper WC
                            req.authname = req.args.get('authname')
                            template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
                            with template_cls(self.env, self.trac_env_name,
                                              req.authname, ticket) as doc:
                                # Update/Upgrade the source file
                                if (ticket['sourcefile'] and ticket['sourcefile'] != 'N/A'):
                                    doc.update_data(ticket['sourcefile'])
                                    doc.upgrade_document(ticket['sourcefile'])
                            sleep(1)

                        elif action == 'commit':

                            # Anonymous request from ProtocolHandler
                            # so we have to supply the authname
                            # which is required to access the proper WC
                            req.authname = req.args.get('authname')
                            template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
                            with template_cls(self.env, self.trac_env_name,
                                              req.authname, ticket) as doc:

                                # Add form fields
                                if ticket['type'] == 'ECM' and not Ticket_UI.get_UI(ticket).legacy:
                                    doc.xform_pdf(ticket['sourcefile'], ticket['pdffile'])

                                # Add attachments
                                if ticket['type'] == 'DOC' and ticket['pdffile'] and ticket['pdffile'] != 'N/A':
                                    if req.args.get('attachments') == 'AttachmentsIncluded':
                                        doc.add_attachments(ticket['pdffile'])

                                # Checks before committing the file(s)
                                if (ticket['sourcefile'] and ticket['sourcefile'] != 'N/A' and
                                    ticket['pdffile'] and ticket['pdffile'] != 'N/A'):
                                    if (doc.get_size(ticket['pdffile']) == 0):
                                        raise HTTPNotFound(_("The PDF File is empty"))
                                    elif (doc.status(ticket['sourcefile'], 'wc-status')['change_status'] == 'modified' and
                                          doc.status(ticket['pdffile'], 'wc-status')['change_status'] != 'modified'):
                                        raise HTTPNotFound(_("The PDF File hasn't changed unlike the Source File"))
                                    elif (doc.get_mtime(ticket['pdffile']) < doc.get_mtime(ticket['sourcefile'])):
                                        raise HTTPNotFound(_("The PDF File is older than the Source File"))

                                # Commit the source and pdf files
                                revision = doc.commit()
                                if revision != '':
                                    # The locks have been automatically removed
                                    if (ticket['sourcefile'] and ticket['sourcefile'] != 'N/A'):
                                        doc.set_access(ticket['sourcefile'])
                                    if (ticket['pdffile'] and ticket['pdffile'] != 'N/A'):
                                        doc.set_access(ticket['pdffile'])
                                    # Beware recursion and cache semaphore !
                                    # Used for ECM/FEE author tracking
                                    ticket['sourceurl'] = '%s?rev=%s' % (
                                        util.get_url(ticket['sourceurl']),
                                        revision)
                                    now = datetime.now(utc)
                                    ticket.save_changes('trac', _('Source Url changed (on behalf of %(user)s)', user=req.authname), now)
                                else:
                                    # Remove the locks
                                    if (ticket['sourcefile'] and ticket['sourcefile'] != 'N/A'):
                                        doc.unlock(ticket['sourcefile'])
                                    if (ticket['pdffile'] and ticket['pdffile'] != 'N/A'):
                                        doc.unlock(ticket['pdffile'])

                        else:
                            template_cls = cache.Ticket_Cache.get_subclass(ticket['type'])
                            with template_cls(self.env, self.trac_env_name,
                                              req.authname, ticket) as doc:

                                if action == 'schedule_lock':
                                    # Schedule an automatic edition mode set
                                    doc.create_flag(ticket['sourcefile'], 'go')

                                elif action == 'unschedule_lock':
                                    # Unschedule an automatic edition mode set
                                    doc.remove_flag(ticket['sourcefile'], 'go')

                                elif action == 'lock':
                                    # Update the working copy
                                    doc.update_wc()
                                    # Setup the locks
                                    if ticket['sourcefile'] and ticket['sourcefile'] != 'N/A':
                                        if ticket['sourcefile'].endswith('.docm'):
                                            generated_pdf = ticket['sourcefile'].replace('.docm', '.pdf')
                                        else:
                                            generated_pdf = None
                                        doc.lock(ticket['sourcefile'])
                                        if generated_pdf:
                                            doc.lock(generated_pdf)
                                        elif ticket['pdffile'] != 'N/A':
                                            doc.lock(ticket['pdffile'])

                                elif action == 'unlock':
                                    # Remove the locks
                                    if (ticket['sourcefile'] and ticket['sourcefile'] != 'N/A'):
                                        doc.unlock(ticket['sourcefile'])
                                    if (ticket['pdffile'] and ticket['pdffile'] != 'N/A'):
                                        doc.unlock(ticket['pdffile'])

            else:
                if action == 'change_comment_edit':

                    comment = req.args.get('edited_comment', '')
                    cnum = int(req.args['cnum_edit'])
                    change = ticket.get_change(cnum)
                    if not (req.authname and req.authname != 'anonymous' and
                            change and change['author'] == req.authname):
                        req.perm(ticket.resource).require('TICKET_EDIT_COMMENT')
                    ticket.modify_comment(change['date'], req.authname, comment)

                elif action in ('mom_lock', 'mom_unlock'):

                    tp_data = form.TicketForm.get_ticket_process_data(self.env, req.authname, ticket)
                    tf = tp_data['ticket_form']
                    if (ticket['type'] == 'MOM' and
                        util.exist_in_repo(self.env, tf.http_url)):
                        if action == 'mom_lock':
                            tf.lock(req.href('ticket', ticket_id))
                        else:
                            tf.unlock()
                    else:
                        raise TracError(_("xhrpost invalid action call: %s" % action))

                elif action == 'regenerate' and ticket['type'] == 'MOM':

                    tp_data = form.TicketForm.get_ticket_process_data(self.env, req.authname, ticket)
                    tft = tp_data['ticket_form_template']
                    tf = tp_data['ticket_form']
                    # Create the empty form from the template form
                    if os.access(tf.content_filename, os.F_OK):
                        os.remove(tf.content_filename)
                    copy(tft.cache_name, tf.content_filename)
                    # prepare the MOM form
                    skills = util.get_ticket_skills(self.env, ticket['skill'])
                    tf.setup(skills, ticket['milestonetag'])
                else:
                    raise TracError(_("xhrpost unknown action: %s" % action))

            req.send(json.dumps(action).encode("utf-8"))

    # INavigationContributor methods

    def get_active_navigation_item(self, req):
        return 'login'

    def get_navigation_items(self, req):
        if req.authname and req.authname != 'anonymous':
            user_profiles = [group.strip() for group in
                             self.config.get('artusplugin', 'user_profiles').
                             split(',')]
            user_roles = [group.strip() for group in
                          self.config.get('artusplugin', 'user_roles').
                          split(',')]

            perm = PermissionSystem(self.env)
            all_permissions = perm.get_all_permissions()

            groups = [group for (subject, group) in all_permissions
                      if subject == req.authname]
            if len(groups) == 0:
                groups = ['authenticated']
            else:
                profiles = set()
                for group in groups:
                    if group in user_profiles:
                        profiles.add(group)
                    elif group in user_roles:
                        for (subject, action) in all_permissions:
                            if subject == group:
                                profiles.add(action)
                groups = [group for group in groups
                          if group not in user_profiles]
                for profile in reversed(user_profiles):
                    if profile in profiles:
                        groups.insert(0, profile)
                        break
            users_link = req.href.admin('general/users')
            profile = groups[0]
            rendered_profile = tag.a(profile, href=users_link)
            if len(groups) > 1:
                user_roles = groups[1:]
                displayed_roles = {}
                for role in user_roles:
                    displayed_roles[role] = role.replace('_', ' ').title()
                role_initials = {}
                for role in user_roles:
                    if role == 'program_manager':
                        role_initials[role] = 'PgM'
                    elif role == 'project_manager':
                        role_initials[role] = 'PjM'
                    else:
                        role_initials[role] = ''.join(item[0] for item in displayed_roles[role].split())
                rendered_roles = [('/', tag.a(role_initials[role], href=users_link, title=displayed_roles[role]))
                                  for role in user_roles]
                yield ('metanav', 'login', tag.span(_('logged in as %(user)s', user=req.authname),
                       ' (', rendered_profile, rendered_roles, ')'))
            else:
                yield ('metanav', 'login', tag.span(_('logged in as %(user)s', user=req.authname),
                       ' (', rendered_profile, ')'))

    # ITemplateProvider methods
    # Used to add the plugin's templates and htdocs

    def get_templates_dirs(self):
        """ Location of Trac templates provided by plugin. """
        from pkg_resources import resource_filename  # @UnresolvedImport
        return [resource_filename('artusplugin', 'templates'),
                resource_filename('artusplugin.buildbot', 'templates'),
                resource_filename('artusplugin.admin', 'templates')]

    def get_htdocs_dirs(self):
        """Return a list of directories with static resources (such as style
        sheets, images, etc.)

        Each item in the list must be a `(prefix, abspath)` tuple. The
        `prefix` part defines the path in the URL that requests to these
        resources are prefixed with.

        The `abspath` is the absolute path to the directory containing the
        resources on the local file system.
        """
        from pkg_resources import resource_filename  # @UnresolvedImport
        return [('artusplugin', resource_filename('artusplugin', 'htdocs'))]

    # IPropertyRenderer methods

    def match_property(self, name, mode):
        return name in ('source:url') and 7 or 0

    def render_property(self, name, mode, context, props):
        if name == 'source:url':
            match = re.search(self.url_regexp, props[name])
            if not match or match.group(1) not in self.url_translation:
                return tag.a(props[name], href=props[name])
            else:
                translated_url = props[name].replace(match.group(1), self.url_translation[match.group(1)])
                return tag.a(props[name], href=translated_url, title=translated_url)
