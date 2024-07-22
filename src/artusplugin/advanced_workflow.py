# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Artus
# All rights reserved.
#
# Author: Michel Guillot <michel.guillot@meggitt.com>

""" Customized workflow derived from AdvancedTicketWorkflow plugin """

# Python-Future
from __future__ import unicode_literals
from future import standard_library
standard_library.install_aliases()
from builtins import zip
from builtins import object

# Genshi
# from genshi.builder import tag
# from genshi.output import DocType
# from genshi.template import MarkupTemplate, TemplateLoader
# from genshi.template.loader import TemplateNotFound
from trac.util.html import html as tag
from jinja2 import Environment, FileSystemLoader, TemplateNotFound

# Trac
from trac.attachment import Attachment
from trac.core import implements, Component, TracError
from trac.ticket import Ticket
from trac.ticket.api import ITicketActionController
from trac.ticket.web_ui import TicketModule
from trac.perm import PermissionSystem
from trac.util.datefmt import utc
from trac.web.chrome import Chrome

# Standard lib
from collections import deque
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email import Encoders
from itertools import groupby, chain
from collections import OrderedDict
from ldap_utilities import Ldap_Utilities
from io import StringIO
import copy
import os
import re
import smtplib
import sys
import urllib.request, urllib.error, urllib.parse
from zipfile import ZipFile

# Same package
from artusplugin import form, _
from artusplugin.admin.web_ui import VersionTagsAdminPanel
from artusplugin.buildbot.web_ui import BuildBotModule
from artusplugin.model import Tag, Document, BaselineItem, Branch
from artusplugin.web_ui import Ticket_UI
from artusplugin.util import OrderedSet, Users
import artusplugin.util as util
import artusplugin.cache as cache


class TicketWF(object):

    @staticmethod
    def get_WF(ticket):
        if ticket['type'] == 'ECM':
            if not ticket.exists or 'ecmtype' in ticket.values:
                ticket_type = 'ECM2'
            else:
                ticket_type = 'ECM1'
        else:
            ticket_type = ticket['type']
        WF_class = getattr(sys.modules['artusplugin.advanced_workflow'], '%sWF' % ticket_type)
        return WF_class

    @staticmethod
    def get_status(ticket):
        old_status = ticket._old.get('status', None)
        status = ticket.values.get('status', None)
        if old_status == 'new' or status == 'new':
            status = TicketWF.get_WF(ticket).get_initial_status()
        elif old_status is not None:
            status = old_status
        # legacy support
        if status in ('04-assigned_for_release', '05-assigned_for_release'):
            status = '06-assigned_for_release'

        return status

    def __init__(self, component, req, ticket, action=None):
        self.component = component
        self.env = component.env
        self.req = req
        self.ticket = ticket
        self.action = action

    def apply_filters(self, operation, users):
        """ Filter users given as input and select a user
            based on  operation
        """
        # Ticket owner
        owner = self.ticket['owner']
        # Pre-selected user
        selected_user = None
        if operation == 'set_owner_to_other':
            users = [u for u in users if u != owner]
        elif operation == 'set_owner_to_role':
            selected_user = self.get_user_with_role()
            if self.with_independence():
                users = [u for u in users if u != owner]
        else:
            # Ticket owner's peer
            peer = self.get_peer()
            if operation == 'set_owner_to_peer':
                if self.with_independence():
                    users = [u for u in users if u != owner]
                if peer in users:
                    selected_user = peer
            elif operation == 'set_owner_to_self':
                if self.with_independence():
                    users = [u for u in users if u != peer]
                if owner in users:
                    selected_user = owner

        return users, selected_user

    def get_allowed_actions(self, **kwargs):
        allowed_actions = OrderedDict()
        actions = [t[1] for t in self.component.get_ticket_actions(self.req, self.ticket)]
        for action in actions:
            allowed_actions[action] = self.is_action_allowed(action, **kwargs)

        return allowed_actions

    def get_approver(self):
        return None

    def get_author(self):
        return None

    def get_confmgr(self):
        return None

    def get_labels(self):
        """Determines the labels"""
        triage_field = self.component.actions[self.action]['triage_field']
        status = self.get_status(self.ticket)
        labels = self.component.actions[self.action]['triage_labels'][self.get_triage_value(triage_field)][status]

        def render_labels(labels):
            for lbl in labels.split(','):
                lbl = lbl.strip()
                if lbl == '*':
                    # status is determined by looking at ticket history
                    new_status = self.get_new_status()
                    yield self.get_reassign_labels()[new_status]
                else:
                    yield lbl

        return [lbl for lbl in render_labels(labels)]

    def get_new_status(self):
        """Determines the new status"""
        action = self.action if self.action else 'reassign'
        status = self.get_status(self.ticket)
        if action in ('view', 'reassign'):
            # status does not change
            new_status = status
        else:
            new_status = self.component.actions[action]['newstate']

            if new_status == '*':
                triage_field = self.component.actions[action]['triage_field']
                new_status = self.component.actions[action]['triage_status'][self.get_triage_value(triage_field)][status]
                if new_status == '*'and action == 'reopen':
                    new_status = self.get_previous_status()
                    # legacy support
                    if new_status == 'assigned_for_closure':
                        new_status = '07-assigned_for_closure_actions'

        return new_status

    def get_operations(self, action=None, status=None):
        """Get the operations for the current action"""
        action = self.action if action is None else action
        status = self.get_status(self.ticket) if status is None else status
        operations = ''
        if 'triage_operations' in self.component.actions[action]:
            triage_field = self.component.actions[action]['triage_field']
            try:
                operations = self.component.actions[action]['triage_operations'][self.get_triage_value(triage_field)][status]
            except Exception as extraInfo:
                message = tag.p("Failed to get the operations for the action ",
                                tag.em("%s" % action),
                                " in the status ",
                                tag.em("%s." % status),
                                class_="message")
                message(tag.p(extraInfo))
                raise TracError(message)

        return [op.strip() for op in operations.split(',')]

    def get_owner(self):
        old_owner = self.ticket._old.get('owner', None)
        owner = self.ticket.values.get('owner', None)
        if old_owner is not None:
            owner = old_owner

        return owner

    def get_owner_hint(self, operation, post_id=None):
        owner_hint = ''

        owner_role = self.get_owner_role()
        peer_role = self.get_peer_role()

        if operation in ('set_owner_to_peer', 'set_owner_to_role'):
            if peer_role:
                if post_id:
                    owner_hint = tag.a(_(' (%s) ' % peer_role),
                                       href="%s/index.php?post/%s" % (self.component.dc_url, post_id),
                                       title="The role of the assignee")
                else:
                    owner_hint = tag.span(_(' (%s) ' % peer_role))
            else:
                owner_hint = tag.span(_(' (ticket owner\'s peer) '))
        elif operation == 'set_owner_to_other':
            owner_hint = tag.span(_(' (ticket owner excluded) '))
        elif operation == 'set_owner_to_self':
            if owner_role:
                if post_id:
                    owner_hint = tag.a(_(' (%s) ' % owner_role),
                                       href="%s/index.php?post/%s" % (self.component.dc_url, post_id),
                                       title="The role of the assignee")
                else:
                    owner_hint = tag.span(_(' (%s) ' % owner_role))

        return owner_hint

    def get_owner_role(self):
        """ Determines the owner's role by analyzing the workflow
            The owner's role is the required role for the current workflow state
        """
        owner_role = None
        current_status = self.get_status(self.ticket)

        for t in list(self.component.actions.items()):
            if t[0] in [self.component.abort_action, 'resolve', 'reassign']:
                continue
            triage_field = t[1]['triage_field']
            if (self.get_triage_value(triage_field) in list(t[1]['triage_permissions'].keys()) and
                (t[1]['oldstates'] == ['*'] or current_status in t[1]['oldstates'])):
                owner_role = self.get_required_role(t[0], current_status)
                break

        return owner_role

    def get_owners_and_selected_owner(self, operation):
        owners = self.get_owners_list()
        owners, selected_owner = self.apply_filters(operation, owners)
        if not selected_owner:
            tag_id = self.action + '_reassign_owner'
            selected_owner = self.req.args.get(tag_id, self.req.authname)

        return owners, selected_owner

    def get_owners_list(self):
        """Get the owners list in case of :
            * set_owner
            * set_owner_to_peer
            * set_owner_to_self
            * set_owner_to_other
            * set_owner_to_role
            operations """
        if self.component.restrict_owner:
            # Required permission for next status is evaluated
            # It is the permission that allows all transitions from that status
            # Only users with that permission will be included
            required_permissions = set(['TICKET_MODIFY'])
            next_status = self.get_new_status()

            for action_name, action_attributes in list(self.component.actions.items()):
                if action_name in [self.component.abort_action, 'reassign']:
                    continue
                triage_field = action_attributes['triage_field']
                if (self.get_triage_value(triage_field) in list(action_attributes['triage_permissions'].keys()) and
                    (action_attributes['oldstates'] == ['*'] or next_status in action_attributes['oldstates'])):
                    permission = self.get_required_permission(action_name, next_status)
                    if permission != 'TICKET_ADMIN':
                        required_permissions.add(permission)

            owners = []
            for user in self.component.users:
                userperms = self.component.perm.get_user_permissions(user)
                add_user = True
                for group in required_permissions:
                    if group not in userperms or userperms[group] is False:
                        add_user = False
                        break
                if add_user:
                    owners.append(user)
            owners.sort()
        else:
            owners = None

        return owners

    def get_peer(self):
        """ Try and get the owner's peer on the ticket history basis only """
        changes = [change for change in
                   self.component.ticket_module.rendered_changelog_entries(self.req, self.ticket)]
        ticket_owner = self.ticket['owner']
        role_assignee = None
        peer = None
        for change in reversed(changes):
            if ('status' in change['fields'] and
                'owner' in change['fields']):
                # Search for a change of user
                if change['fields']['status']['old'] == 'closed':
                    # role assignee changed when ticket was re-opened
                    role_assignee = change['fields']['owner']['old']
                else:
                    if change['fields']['owner']['new'] in (ticket_owner, role_assignee):
                        peer = change['fields']['owner']['old']
                        break
                    elif change['fields']['owner']['old'] == ticket_owner:
                        peer = change['fields']['owner']['new']
                        break
        else:
            # No change of user found in history
            if self.ticket['type'] == 'PRF':
                if self.ticket['status'] in ('01-assigned_for_description', '04-analysed', '06-implemented'):
                    # PRF owner is the reviewer, peer is the author
                    parent = self.ticket.get_value_or_default('parent')
                    if parent:
                        # DOC/ECM/FEE ticket
                        tkt = Ticket(self.env, parent.lstrip('#'))
                        # As the PRF is not closed, the author should be the parent ticket owner 
                        # because all PRFs have to be closed before leaving edition or review status
                        peer = tkt['owner'] 
                else:
                    # PRF owner is the author, peer is the reviewer and is also the PRF owner
                    # as no change of user has been found in history
                    peer = ticket_owner

        return peer

    def get_peer_role(self):
        """ Determines the role of the peer by analyzing the workflow
            The peer role is the required role for the workflow state to which the given action leads
        """
        peer_role = None
        next_status = self.get_new_status()

        for t in list(self.component.actions.items()):
            if t[0] in [self.component.abort_action, 'resolve', 'reassign']:
                continue
            triage_field = t[1]['triage_field']
            if (self.get_triage_value(triage_field) in list(t[1]['triage_permissions'].keys()) and
                (t[1]['oldstates'] == ['*'] or next_status in t[1]['oldstates'])):
                peer_role = self.get_required_role(t[0], next_status)
                break

        return peer_role

    def get_previous_status(self, status=None, force_backwards=False):
        previous_status = None
        current_status = self.get_status(self.ticket) if status is None else status
        if self.action in ['view', 'reassign', 'change_resolution']:
            previous_status = current_status
        else:
            changes = [change for change in
                       self.component.ticket_module.rendered_changelog_entries(self.req, self.ticket)]
            for change in reversed(changes):
                if 'status' in change['fields']:
                    old_status = change['fields']['status']['old']
                    new_status = change['fields']['status']['new']
                    if (new_status == current_status and
                        (not force_backwards or (old_status != 'closed' and (new_status == 'closed' or new_status > old_status)))):
                        previous_status = old_status
                        # legacy support
                        if previous_status in ("04-assigned_for_release", "05-assigned_for_release"):
                            previous_status = "06-assigned_for_release"
                        break

        return previous_status

    def get_previous_tag(self, current_tg):
        changes = [change for change in
                   self.component.ticket_module.rendered_changelog_entries(self.req, self.ticket)]
        applied_regexp = '^Tag (.+) applied$'
        removed_regexp = '^Tag (.+) removed$'
        removed_tags = [current_tg]
        for change in reversed(changes):
            match = re.search(removed_regexp, change['comment'])
            if match:
                tg = match.group(1)
                removed_tags.append(tg)
            match = re.search(applied_regexp, change['comment'])
            if match:
                tg = match.group(1)
                if tg not in removed_tags:
                    previous_tg = tg
                    break
        else:
            previous_tg = None

        return previous_tg

    def get_projmgr(self):
        return None

    def get_reassign_labels(self):
        reassign_labels = {}

        reassign_labels['01-assigned_for_description'] = "reassign for description"
        reassign_labels['02-described'] = "reassign for description validation"
        reassign_labels['03-assigned_for_analysis'] = "reassign for analysis"
        reassign_labels['04-analysed'] = "reassign for analysis validation"
        reassign_labels['05-assigned_for_implementation'] = "reassign for implementation"
        reassign_labels['06-implemented'] = "reassign for implementation verification"
        reassign_labels['07-assigned_for_closure_actions'] = "reassign for closure actions"

        return reassign_labels

    def get_required_permission(self, action=None, status=None):
        action = self.action if action is None else action
        required_permission = None
        if 'triage_permissions' in self.component.actions[action]:
            triage_field = self.component.actions[action]['triage_field']
            if self.get_triage_value(triage_field) in self.component.actions[action]['triage_permissions']:
                if not status:
                    status = self.get_status(self.ticket)
                required_permission = self.component.actions[action]['triage_permissions'][self.get_triage_value(triage_field)][status]

        return required_permission

    def get_required_role(self, action=None, status=None):
        action = self.action if action is None else action
        required_role = None
        if 'triage_roles' in self.component.actions[action]:
            triage_field = self.component.actions[action]['triage_field']
            if self.get_triage_value(triage_field) in self.component.actions[action]['triage_roles']:
                if not status:
                    status = self.get_status(self.ticket)
                required_role = self.component.actions[action]['triage_roles'][self.get_triage_value(triage_field)][status]

        return required_role

    def get_resolution(self):
        old_resolution = self.ticket._old.get('resolution', None)
        resolution = self.ticket.values.get('resolution', None)
        if old_resolution is not None:
            resolution = old_resolution

        return resolution

    def get_reviewer(self):
        return None

    def get_roles_by_initials(self, name):
        # Get all roles for the given name
        # dictionary: eg key: SCM / value: Software Configuration Manager
        roles_by_initials = {}
        for (subject, group) in self.component.all_permissions:
            if subject == name and group in self.component.user_roles:
                value = group.replace('_', ' ').title()
                if value == 'Project Manager':
                    key = 'PjM'
                elif value == 'Program Manager':
                    key = 'PgM'
                else:
                    key = "".join(item[0] for item in value.split())
                roles_by_initials[key] = value

        return roles_by_initials

    def get_states_for_role(self, role):
        states = []
        for _, action_atributes in list(self.component.actions.items()):
            triage_field = action_atributes['triage_field']
            if 'triage_roles' in action_atributes and self.get_triage_value(triage_field) in action_atributes['triage_roles']:
                states += [s for s, r in list(action_atributes['triage_roles'][self.get_triage_value(triage_field)].items()) if r == role]

        # unicity
        return set(states)

    def get_triage_value(self, triage_field):
        return self.ticket[triage_field]

    def get_role_from_permission(self, permission):
        # See trac.ini
        if permission == 'TICKET_MODIFY':
            return 'authenticated'
        elif permission == 'TICKET_CREATE':
            return 'developer'
        elif permission == 'MILESTONE_CREATE':
            return 'authorized'
        elif permission == 'TICKET_ADMIN':
            return 'admin'

    def get_user_with_role(self):
        """ Determines peer user by searching backwards in ticket history
            The user is the one who assigned the ticket
            from a workflow state associated with the peer role
            Only forward progress in the workflow is considered
        """
        user_with_role = None
        changes = [change for change in
                   self.component.ticket_module.rendered_changelog_entries(self.req, self.ticket)]
        peer_role = self.get_peer_role()
        if peer_role:
            states_for_role = self.get_states_for_role(peer_role)
            ticket_owner = self.ticket['owner']
            for change in reversed(changes):
                if ('owner' in change['fields']):
                    # owner tracking
                    ticket_owner = change['fields']['owner']['old']
                if ('status' in change['fields']):
                    old_status = change['fields']['status']['old']
                    new_status = change['fields']['status']['new']
                    if (old_status in states_for_role and
                        new_status > old_status):
                        user_with_role = ticket_owner
                        break

        return user_with_role

    def is_action_allowed(self, action=None, **kwargs):
        action = self.action if action is None else action

        action_allowed = self.is_action_allowed_prologue(action)
        action_allowed = self.is_action_allowed_core(action_allowed, action, **kwargs)

        return action_allowed

    def is_action_allowed_prologue(self, action=None):
        action = self.action if action is None else action

        # For almost all actions, ticket ownership combined with
        # user profile determine if the action is allowed
        required_permission = self.get_required_permission(action)
        required_profile = self.get_role_from_permission(required_permission)
        if required_permission == 'TICKET_ADMIN':
            if required_permission in self.req.perm(self.ticket.resource):
                action_allowed = (True, "Having the %s profile allows this action" % required_profile)
            else:
                action_allowed = (False, "This action requires an %s profile" % required_profile)
        else:
            if self.req.authname == self.ticket['owner']:
                if required_permission in self.req.perm(self.ticket.resource):
                    action_allowed = (True, "Being owner and having (at least) the %s profile allow this action" % required_profile)
                else:
                    action_allowed = (False, "This action requires at least the %s profile" % required_profile)
            else:
                action_allowed = (False, "This action requires being the ticket owner")

        return action_allowed

    def is_action_allowed_core(self, action_allowed, action=None, **kwargs):
        action = self.action if action is None else action

        # For some actions, user profile allows the action
        # even if the user is not the owner of the ticket
        if self.req.authname != self.ticket['owner'] and action in ('reassign', self.component.abort_action, 'change_resolution'):
            if action == 'reassign':
                required_permission = 'TICKET_CREATE'
                required_profile = self.get_role_from_permission(required_permission)
            else:
                required_permission = self.get_required_permission(action)
                required_profile = self.get_role_from_permission(required_permission)
            if required_permission in self.req.perm(self.ticket.resource):
                action_allowed = (True, "Although not being owner, having (at least) the %s profile allows this action" % required_profile)
            else:
                action_allowed = (False, "Not being owner, this action requires at least the %s profile" % required_profile)

        return action_allowed

    def is_action_filtered(self):
        return False

    def send_emails(self):
        return

    def set_tag(self):
        return

    def sign_document(self):
        return

    def unset_tag(self):
        return

    def update_document(self):
        return

    def with_independence(self):
        return False


class FormWF(TicketWF):

    @staticmethod
    def get_initial_status():
        return u'01-assigned_for_description'


class EFRWF(FormWF):

    def get_activities(self, **kwargs):
        """ Get ticket current activity """
        activities = {}

        activities['01-assigned_for_description'] = _("Description Action")
        activities['02-described'] = _("Description Validation")
        activities['03-assigned_for_analysis'] = _("Analysis Action")
        activities['04-analysed'] = _("Analysis Validation")
        activities['07-assigned_for_closure_actions'] = _("Closure Actions")
        activities['closed'] = _("None")

        return activities

    def get_author(self):
        """
        Get the author
        """
        if self.ticket['status'] in ('01-assigned_for_description', '03-assigned_for_analysis'):
            author = self.ticket['owner']
        else:
            author = self.get_peer()

        return author

    def get_reviewer(self):
        """
        Get the reviewer
        """
        if self.ticket['status'] in ('01-assigned_for_description', '03-assigned_for_analysis'):
            reviewer = self.get_peer()
        else:
            reviewer = self.ticket['owner']

        return reviewer

class ECRWF(FormWF):

    def get_activities(self, **kwargs):
        """ Get ticket current activity """
        activities = {}

        activities['01-assigned_for_description'] = _("Description Action")
        activities['02-described'] = _("Description Validation")
        activities['03-assigned_for_analysis'] = _("Analysis Action")
        activities['04-analysed'] = _("Analysis Validation")
        activities['05-assigned_for_implementation'] = _("Implementation Action")
        activities['06-implemented'] = _("Implementation Validation")
        activities['07-assigned_for_closure_actions'] = _("Closure Actions")
        activities['closed'] = _("None")

        return activities

    def get_author(self):
        """
        Get the author
        """
        if self.ticket['status'] in ('01-assigned_for_description', '03-assigned_for_analysis', '05-assigned_for_implementation'):
            author = self.ticket['owner']
        else:
            author = self.get_peer()

        return author

    def get_reviewer(self):
        """
        Get the reviewer
        """
        if self.ticket['status'] in ('01-assigned_for_description', '03-assigned_for_analysis', '05-assigned_for_implementation'):
            reviewer = self.get_peer()
        else:
            reviewer = self.ticket['owner']

        return reviewer


class RFWF(FormWF):

    def apply_filters(self, operation, users):
        """ Filter users given as input and select a user
            based on  operation
        """
        # RF owner (author or reviewer)
        owner = self.ticket['owner']
        # Pre-selected user (author or reviewer)
        selected_user = None
        if operation == 'set_owner_to_other':
            users = [u for u in users if u != owner]
        elif operation == 'set_owner_to_role':
            selected_user = self.get_user_with_role()
            if self.with_independence():
                users = [u for u in users if u != owner]
        else:
            # RF owner's peer (reviewer or author)
            peer = self.get_peer()
            # Filter reviewer
            if operation == 'set_owner_to_peer' and self.get_peer_role() == 'reviewer':
                users = [peer]
                selected_user = peer
            elif operation == 'set_owner_to_self' and self.get_owner_role() == 'reviewer':
                users = [owner]
                selected_user = owner
            else:
                # Filter authors
                if operation == 'set_owner_to_peer':
                    if self.with_independence():
                        users = [u for u in users if u != owner]
                    if peer in users:
                        selected_user = peer
                elif operation == 'set_owner_to_self':
                    if self.with_independence():
                        users = [u for u in users if u != peer]
                    if owner in users:
                        selected_user = owner

        return users, selected_user

    def get_activities(self, **kwargs):
        """ Get ticket current activity """
        activities = {}

        activities['01-assigned_for_description'] = _("Description Action")
        activities['02-described'] = _("Description Validation")
        activities['03-assigned_for_analysis'] = _("Analysis Action")
        activities['04-analysed'] = _("Analysis Validation")
        activities['05-assigned_for_implementation'] = _("Implementation Action")
        activities['06-implemented'] = _("Implementation Validation")
        activities['07-assigned_for_closure_actions'] = _("Closure Actions")
        activities['closed'] = _("None")

        return activities

    def get_author(self):
        """
        Get the author
        """
        if self.ticket['status'] in ('01-assigned_for_description', '04-analysed', '06-implemented'):
            author = self.get_peer()
        else:
            author = self.ticket['owner']

        return author

    def get_reviewer(self):
        """
        Get the reviewer
        """
        if self.ticket['status'] in ('01-assigned_for_description', '04-analysed', '06-implemented'):
            reviewer = self.ticket['owner']
        else:
            reviewer = self.get_peer()

        return reviewer

    def get_owner_hint(self, operation, post_id='98'):
        return super(RFWF, self).get_owner_hint(operation, post_id)

    def is_action_allowed_core(self, action_allowed, action=None, **kwargs):
        action = self.action if action is None else action

        action_allowed = super(RFWF, self).is_action_allowed_core(action_allowed, action)

        if (action == self.component.abort_action or
            (action in ('resolve', 'change_resolution') and
             self.get_required_role(action) and
             self.with_independence())):
            if self.req.authname == self.get_author():
                action_allowed = (True, "This action is allowed for the author")
            elif self.req.authname == self.get_reviewer():
                action_allowed = (False, "This action is not allowed for the reviewer")
            elif 'MILESTONE_CREATE' in self.req.perm(self.ticket.resource):
                action_allowed = (True, "This action is allowed for an authorized third party")
            else:
                action_allowed = (False, "This action is only allowed for the author or an authorized third party")

        return action_allowed

    def with_independence(self):
        with_independence = False
        try:
            document = Document(
                self.env, Tag(self.env, self.ticket['document']).tracked_item)
            if document['independence'] in (None, 1):
                with_independence = True
        except Exception:
            pass

        return with_independence


class MOMWF(FormWF):

    def get_activities(self, **kwargs):
        """ Get ticket current activity """
        activities = {}

        activities['01-assigned_for_description'] = _("Description Action")
        activities['07-assigned_for_closure_actions'] = _("Closure Actions")
        activities['closed'] = _("None")

        return activities

    def is_action_allowed_core(self, action_allowed, action=None, **kwargs):
        action = self.action if action is None else action

        action_allowed = super(MOMWF, self).is_action_allowed_core(action_allowed, action)

        tp_data = form.TicketForm.get_ticket_process_data(self.env, self.req.authname, self.ticket)
        tf = tp_data['ticket_form']
        if util.exist_in_repo(self.env, tf.http_url) and tf.lock_status():
            action_allowed = (False, "No action is allowed when locked")

        return action_allowed


class RISKWF(FormWF):

    def get_activities(self, **kwargs):
        """ Get ticket current activity """
        activities = {}

        activities['01-assigned_for_description'] = _("Description Action")
        activities['07-assigned_for_closure_actions'] = _("Closure Actions")
        activities['closed'] = _("None")

        return activities


class AIWF(FormWF):

    def get_activities(self, **kwargs):
        """ Get ticket current activity """
        activities = {}

        activities['01-assigned_for_description'] = _("Description Action")
        activities['05-assigned_for_implementation'] = _("Implementation Action")
        activities['07-assigned_for_closure_actions'] = _("Closure Actions")
        activities['closed'] = _("None")

        return activities


class PRFWF(FormWF):

    def apply_filters(self, operation, users):
        """ Filter users given as input and select a user
            based on  operation
        """
        # PRF owner (author or reviewer)
        owner = self.ticket['owner']
        # Pre-selected user (author or reviewer)
        selected_user = None
        if operation == 'set_owner_to_other':
            users = [u for u in users if u != owner]
        elif operation == 'set_owner_to_role':
            selected_user = self.get_user_with_role()
            if self.ticket['status'] != 'closed' and self.with_independence():
                users = [u for u in users if u != owner]
        else:
            # PRF owner's peer (reviewer or author)
            peer = self.get_peer()
            # Filter reviewer
            if operation == 'set_owner_to_peer' and self.get_peer_role() == 'reviewer':
                users = [peer]
                selected_user = peer
            elif operation == 'set_owner_to_self' and self.get_owner_role() == 'reviewer':
                users = [owner]
                selected_user = owner
            # Filter authors
            else:
                parent = self.ticket.get_value_or_default('parent')
                if parent:
                    # DOC/ECM/FEE ticket
                    tkt = Ticket(self.env, parent.lstrip('#'))
                    # DOC/ECM/FEE authors
                    users = TicketWF.get_WF(tkt).get_authors(tkt)
                if operation == 'set_owner_to_peer':
                    # peer (author) may change from what it was based on history
                    # if review is piloted through a DOC ticket
                    if self.with_independence():
                        users = [u for u in users if u != owner]
                    if parent:
                        peer = tkt['owner']
                    if peer in users:
                        selected_user = peer
                elif operation == 'set_owner_to_self':
                    # peer (reviewer) won't change as PRF ticket has been named
                    # according to the name of the reviewer
                    if self.with_independence():
                        users = [u for u in users if u != peer]
                    if parent and tkt['owner'] in users:
                            selected_user = tkt['owner']
                    elif owner in users:
                            selected_user = owner

        return users, selected_user

    def get_activities(self, **kwargs):
            """ Get ticket current activity """
            activities = {}

            activities['01-assigned_for_description'] = _("Description Action")
            activities['02-described'] = _("Description Validation")
            activities['03-assigned_for_analysis'] = _("Analysis Action")
            activities['04-analysed'] = _("Analysis Validation")
            activities['05-assigned_for_implementation'] = _("Implementation Action")
            activities['06-implemented'] = _("Implementation Validation")
            activities['07-assigned_for_closure_actions'] = _("Closure Actions")
            activities['closed'] = _("None")

            return activities

    def get_author(self):
        """
        Get the author
        """
        if self.ticket['status'] in ('01-assigned_for_description', '04-analysed', '06-implemented'):
            parent = self.ticket.get_value_or_default('parent')
            if parent:
                # DOC/ECM/FEE ticket
                tkt = Ticket(self.env, parent.lstrip('#'))
                author = tkt['owner']
            else:
                author = self.get_peer()
        else:
            author = self.ticket['owner']

        return author

    def get_reviewer(self):
        """
        Get the reviewer
        """
        if self.ticket['status'] in ('01-assigned_for_description', '04-analysed', '06-implemented'):
            reviewer = self.ticket['owner']
        else:
            reviewer = self.get_peer()

        return reviewer

    def get_owner_hint(self, operation, post_id='98'):
        return super(PRFWF, self).get_owner_hint(operation, post_id)

    def is_action_allowed_core(self, action_allowed, action=None, **kwargs):
        action = self.action if action is None else action

        action_allowed = super(PRFWF, self).is_action_allowed_core(action_allowed, action)

        if (action == self.component.abort_action or
            (action in ('resolve', 'change_resolution') and
             self.get_required_role(action) and
             self.with_independence())):
            if self.req.authname == self.get_author():
                action_allowed = (True, "This action is allowed for the author")
            elif self.req.authname == self.get_reviewer():
                action_allowed = (False, "This action is not allowed for the reviewer")
            elif 'MILESTONE_CREATE' in self.req.perm(self.ticket.resource):
                action_allowed = (True, "This action is allowed for an authorized third party")
            else:
                action_allowed = (False, "This action is only allowed for the author or an authorized third party")

        return action_allowed

    def signature_agreement(self):
        agreement = False
        if self.ticket['parent']:
            tkt = Ticket(self.env, self.ticket['parent'].lstrip('#'))
            if tkt['type'] == 'DOC':
                # PRF is child of a DOC ticket
                # Signature agreement is only given
                # when the document has been accepted as is (no implementation)
                if self.ticket['status'] in ('01-assigned_for_description', '04-analysed'):
                    # Signature agreement is only given
                    # when the document is Released
                    if 'Proposed' in self.ticket['summary']:
                        agreement = True
            elif tkt['type'] in ('ECM', 'FEE'):
                # PRF is child of an ECM/FEE ticket
                agreement = True

        return agreement

    def with_independence(self):
        with_independence = False
        try:
            document = Document(
                self.env, Tag(self.env, self.ticket['document']).tracked_item)
            if document['independence'] in (None, 1):
                with_independence = True
        except Exception:
            pass

        return with_independence


class MEMOWF(FormWF):

    def get_activities(self, **kwargs):
        """ Get ticket current activity """
        activities = {}

        activities['01-assigned_for_description'] = _("Description Action")
        activities['07-assigned_for_closure_actions'] = _("Closure Actions")
        activities['closed'] = _("None")

        return activities


class ECM1WF(FormWF):

    def get_activities(self, **kwargs):
        """ Get ticket current activity """
        activities = {}

        activities['01-assigned_for_description'] = _("Description Action")
        activities['07-assigned_for_closure_actions'] = _("Closure Actions")
        activities['closed'] = _("None")

        return activities

    def get_new_status(self):
        """Determines the new status"""
        action = self.action if self.action else 'reassign'
        status = self.get_status(self.ticket)
        new_status = self.component.actions[action]['newstate']

        if new_status == '*':
            triage_field = self.component.actions[action]['triage_field']
            new_status = self.component.actions[action]['triage_status'][self.get_triage_value(triage_field)][status]
            if new_status == '*':
                if action == 'reopen':
                    new_status = self.get_previous_status()
                    # legacy support
                    if new_status == 'assigned_for_closure':
                        new_status = '07-assigned_for_closure_actions'
                else:
                    # 'view' or 'reassign' actions only: status does not change
                    new_status = status

        return new_status

    def get_triage_value(self, triage_field):
        return '%s1' % self.ticket[triage_field]


class DocWF(TicketWF):

    @staticmethod
    def get_initial_status():
        return u'01-assigned_for_edition'

    def apply_filters(self, operation, users):
        """ Filter users given as input and select a user
            based on  operation
        """
        # Ticket owner
        owner = self.ticket['owner']
        # Pre-selected user
        selected_user = None
        if operation == 'set_owner_to_role':
            selected_user = self.get_user_with_role()
            if self.action in ('abort_approval', 'approve', 'abort_release', 'release'):
                users = TicketWF.get_WF(self.ticket).get_authors(self.ticket)
            elif self.with_independence():
                users = [u for u in users if u != owner]

        return users, selected_user

    def get_approvers(self):
        return []

    def get_approvers_count(self):
        # Number of approvers that already signed (based on history)
        approvers_count = len(self.get_approvers())
        return approvers_count

    @staticmethod
    def get_authors():
        return OrderedSet()

    def get_signed_as(self, signatures):
        signers = {signer_role: ','.join(['%s (%s)' % (signature['user'], signature['role'])
                                          for signature in signatures
                                          if signature['signer_status'] == signer_role])
                   for signer_role in ('author', 'reviewer', 'approver', 'sender')}
        return ['%s by %s' % (signer_role, signers[signer_role]) if signers[signer_role] else '' for signer_role in ('author', 'reviewer', 'approver', 'sender')]

    def get_signature_timestamp(self):
        return False

    def get_signatures(self):
        """ Get signatures to apply for given ticket and action
          In all:
           -> there is one author
           -> there may be one or several reviewers
           -> there may be one or several approvers
           -> there may be one sender
          For the given action there may be:
           -> one author and/or
           -> one or several reviewers and/or
              (all reviewers signatures are applied atomically that is in one action)
           -> one approver
              (approvers signatures are applied one by one that is through several actions)
           -> one sender """

        signatures = []

        signers = self.get_signers_to_sign()

        reviewers_count = 0

        # RE to be applied to signer description
        regexp = r'^([A-Za-z&]+) ([a-z-]+\.[a-z-]+)'

        for (signer_status, signer_description) in signers:
            # eg: signer_status: author / signer_description: SCM michel.guillot
            signature = {}
            signature['signer_status'] = signer_status
            if signer_status == 'author':
                signature['rect_id'] = 1
                signature['signed_action'] = 'Written'
            elif signer_status == 'reviewer':
                signature['rect_id'] = 2 + reviewers_count
                signature['signed_action'] = 'Checked'
                reviewers_count += 1
            elif signer_status == 'approver':
                signature['rect_id'] = 2 + self.get_reviewers_count() + self.get_approvers_count()
                signature['signed_action'] = 'Approved'
            elif signer_status == 'sender':
                signature['rect_id'] = 2 + self.get_reviewers_count() + self.get_approvers_count()
                signature['signed_action'] = 'Sent'
            match = re.search(regexp, signer_description)
            if match:
                signature['role'] = match.group(1)
                signature['user'] = match.group(2)
            else:
                signature['role'] = 'role not set'
                signature['user'] = signer_description.split(' ')[-1]
            signatures.append(signature)

        return signatures

    def get_signers_to_sign(self, action=None, status=None, operation=None):
        """ Get signers whose signatures have to be applied
            after action has been completed """
        action = self.action if action is None else action
        signers = []

        if operation:
            operations = [operation]
        else:
            operations = self.get_operations(action, self.get_previous_status(status,
                            force_backwards=True if self.ticket['type'] in ('DOC', 'ECM') else False))

        if 'sign_as_author' in operations:
            signers.append(('author', '%s %s' % (
                self.req.args.get(action + '_set_role', '(role not set)'),
                self.req.authname)))
        if 'apply_reviewers_signatures' in operations:
            # Child tickets for current or previous version status
            childtickets = util.child_tickets_for_tag(self.ticket, self.get_reviewed_tag())
            signers += [('reviewer', tkt['signer']) for tkt in childtickets
                        if tkt['signer'] and tkt['resolution'] == 'fixed']
        if 'sign_as_approver' in operations:
            current_approver = '%s %s' % (
                self.req.args.get(action + '_set_role', '(role not set)'),
                self.req.authname)
            previous_approvers = self.get_approvers()
            if current_approver not in previous_approvers:
                signers.append(('approver', current_approver))
        if 'sign_as_sender' in operations:
            signers.append(('sender', '%s %s' % (
                self.req.args.get(action + '_set_role', '(role not set)'),
                self.req.authname)))

        return signers

    def sign_document(self):
        # sign as Written, Checked, Approved, Sent
        if (self.signing_in_operations(self.get_previous_status()) and '.' in self.req.authname and
            (self.ticket['type'] != 'DOC' or
             (self.ticket['pdffile'] and self.ticket['pdffile'] != 'N/A' and
              self.ticket['pdfsigned'] == '1'))):

            signatures = self.get_signatures()

            if signatures:

                certificate_validity_required = self.get_certificate_validity_required() == 'True'

                # Check the validity dates of the certificate chain
                sign_jar_path = self.env.config.get('artusplugin', 'sign_jar_path', '/srv/trac/common/TracPdfSign.jar')
                unix_cmd_list = ['sudo /usr/bin/java '
                                 '-jar %s '
                                 '-validity' % sign_jar_path]

                # Effective application of the list of commands
                retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())

                if retcode != 0:
                    if certificate_validity_required:
                        message = tag.p("The check of the validity dates of the certificate chain has failed:",
                                        class_="message")
                        for line in lines:
                            message(tag.p(line))
                        raise TracError(message)
                    else:
                        message = tag.p("", class_="message")
                        for line in lines:
                            if 'GRAVE' in line:
                                message = tag.p(line[len('GRAVE: '):])
                        self.req.chrome['warnings'].append(message)
                        self.req.args['preview'] = True

                signature_timestamp = self.get_signature_timestamp()

                template_cls = cache.Ticket_Cache.get_subclass(self.ticket['type'])
                with template_cls(self.env,
                                  self.component.trac_env_name,
                                  self.req.authname,
                                  self.ticket) as doc:

                    doc.checkout(self.ticket['pdffile'])
                    if self.ticket['sourcefile'] and self.ticket['sourcefile'] != 'N/A':
                        doc.checkout(self.ticket['sourcefile'])

                    # Create sub-directory for signing
                    sign_dir = "%s/.sign" % doc.path
                    if not os.path.exists(sign_dir):
                        os.makedirs(sign_dir)

                    src = "%s/%s" % (doc.path, self.ticket['pdffile'])
                    dest = '%s/.sign/%s' % (doc.path, self.ticket['pdffile'])

                    if self.ticket['sourcefile'] and self.ticket['sourcefile'].endswith('.docm'):
                        tmpl_properties_path = "%s/.sign/TemplateDOC.properties" % doc.path

                        # Sign rectangles coordinates
                        with open(tmpl_properties_path, 'w') as f:
                            f.write("# TemplateDOC properties\n")
                            f.write("# sign rectangles (lower left x, lower left y) "
                                    "(upper right x, upper right y)\n")
                            sign_rectangles = doc.get_sign_rectangles(self.ticket['sourcefile'])
                            for i, ((llXValue, llYValue),
                                    (urXValue, urYValue)) in enumerate(sign_rectangles,
                                                                       start=1):
                                f.write("signrectangle%s=%s\n" % (i, llXValue.text))
                                f.write("signrectangle%s=%s\n" % (i, llYValue.text))
                                f.write("signrectangle%s=%s\n" % (i, urXValue.text))
                                f.write("signrectangle%s=%s\n" % (i, urYValue.text))
                    else:
                        tmpl_properties_path = None

                    # Sign PDF
                    unix_cmd_list = []
                    with Ldap_Utilities() as ldap_util:
                        for signature in signatures:
                            signer_role = signature['role'] if signature['role'] != 'role not set' else ''
                            signer_email = util.Users.get_email(self.env, signature['user'], ldap_util)
                            unix_cmd = ('sudo /usr/bin/java '
                                        '-jar %s '
                                        '-sign ' % sign_jar_path)
                            if tmpl_properties_path:
                                unix_cmd += ('-p "%s" ' % tmpl_properties_path)
                            unix_cmd += ('-A "%s" '
                                         '-a "%s" '
                                         '-R "%s" '
                                         '-e "%s" '
                                         '-r "%s" '
                                         '-t "%s" '
                                         '-s "%s" '
                                         '-d "%s"' % (
                                             signature['signed_action'],
                                             signature['user'],
                                             signer_role,
                                             signer_email,
                                             signature['rect_id'],
                                             signature_timestamp,
                                             src,
                                             dest))
                            unix_cmd_list += [unix_cmd]

                            # Copy signed PDF to the working copy (if not empty)
                            unix_cmd_list += ['if [[ -s "%s" ]] ; then cp -f "%s" "%s"; fi'
                                              % (dest, dest, src)]

                    # Commit
                    unix_cmd_list += [util.SVN_TEMPLATE_CMD % {
                                      'subcommand': 'commit -m "%s" "%s"' % (
                                          _('ticket:%(id)s (on behalf of %(user)s)',
                                            id=str(self.ticket.id), user=self.req.authname), doc.path)}]

                    # Effective application of the list of commands
                    retcode, lines = util.unix_cmd_apply(self.env, unix_cmd_list, util.lineno())

                    if retcode == 0:
                        revision = ''
                        for line in lines:
                            if line.startswith(u'Révision '):
                                regular_expression = u'\ARévision (\d+) propagée\.\n\Z'
                                match = re.search(regular_expression, line)
                                if match:
                                    revision = match.group(1)
                                break

                        if revision != '':
                            self.ticket['sourceurl'] = '%s?rev=%s' % (
                                util.get_url(self.ticket['sourceurl']),
                                revision)
                            now = datetime.now(utc)
                            comment = self.get_signature_comment(doc, signatures)
                            self.ticket.save_changes('trac', comment, now)
                    else:
                        message = tag.p("Ticket ",
                                        tag.a("#%s" % self.ticket.id,
                                              href="%s" % self.req.href.ticket(self.ticket.id)),
                                        ": Failure to sign the document "
                                        "or commit the signed document.",
                                        class_="message")
                        for line in lines:
                            message(tag.p(line))
                        raise TracError(message)

    def signing_in_operations(self, previous_status):
        operations = self.get_operations(self.action, previous_status)
        if set(['sign_as_author', 'apply_reviewers_signatures', 'sign_as_approver', 'sign_as_sender']) & set(operations):
            return True
        else:
            return False


class ECM2WF(DocWF):

    @staticmethod
    def max_reviewers(ticket):
        # The number of reviewers that can sign depends on:
        #       - the signatures reserved internally:
        #           - author
        #           - sender
        #           - trade compliance if not delegated to the sender
        tc_approval_required = ticket.env.config.getbool('artusplugin', 'tc_approval_required', False)
        tc_approval_delegated = ticket.env.config.getbool('artusplugin', 'tc_approval_delegated', False)
        available_signature_boxes = 6
        reserved_signature_boxes = 3 if (tc_approval_required and not tc_approval_delegated) else 2
        max_reviewers = available_signature_boxes - reserved_signature_boxes

        return max_reviewers

    @staticmethod
    def max_reviewers_reached(ticket, childtickets):
        max_reviewers_reached = False

        # Max number of reviewers always managed
        max_reviewers = ECM2WF.max_reviewers(ticket)
        current_reviewers = len([ct for ct in childtickets if ct['type'] == 'PRF' and ct['document'] == ticket['document']
                                 and (ct['status'] != 'closed' or ct['resolution'] != 'rejected')])
        if current_reviewers == max_reviewers:
            max_reviewers_reached = True

        return max_reviewers_reached

    @staticmethod
    def max_reviewers_reached_message(ticket):
        tc_approval_required = ticket.env.config.getbool('artusplugin', 'tc_approval_required', False)
        tc_approval_delegated = ticket.env.config.getbool('artusplugin', 'tc_approval_delegated', False)
        if tc_approval_required and not tc_approval_delegated:
            message = 'You cannot create an additional PRF because two signatures must remain available (trade compliance and sending)'
        elif not tc_approval_required or tc_approval_delegated:
            message = 'You cannot create an additional PRF because one signature must remain available (sending)'

        return message

    @staticmethod
    def too_many_reviewers(ticket, childtickets):
        too_many_reviewers = False

        # Max number of reviewers outgrown
        max_reviewers = ECM2WF.max_reviewers(ticket)
        current_reviewers = len([ct for ct in childtickets if ct['type'] == 'PRF' and ct['document'] == ticket['document']
                                 and (ct['status'] != 'closed' or ct['resolution'] != 'rejected')])
        if current_reviewers > max_reviewers:
            too_many_reviewers = True

        return too_many_reviewers

    @staticmethod
    def too_many_reviewers_message(ticket):
        tc_approval_required = ticket.env.config.getbool('artusplugin', 'tc_approval_required', False)
        tc_approval_delegated = ticket.env.config.getbool('artusplugin', 'tc_approval_delegated', False)
        if tc_approval_required and not tc_approval_delegated:
            message = 'Too many PRF have been created, you must delete or reject some of them because two signatures must remain available (trade compliance and sending)'
        elif not tc_approval_required or tc_approval_delegated:
            message = 'Too many PRF have been created, you must delete or reject some of them because one signature must remain available (sending)'

        return message

    @staticmethod
    def childticket_creation_message(ticket, childtickets):
        if ECM2WF.max_reviewers_reached(ticket, childtickets):
            return ECM2WF.max_reviewers_reached_message(ticket)
        elif ECM2WF.too_many_reviewers(ticket, childtickets):
            return ECM2WF.too_many_reviewers_message(ticket)
        else:
            return ''

    def format_header(self, data):

        header = {}
        header['Sender'] = '"%s" <%s>' % (data['sendername'], data['senderemail'])
        header['From'] = header['Sender']
        header['To'] = '"%s" <%s>' % (data['toname'], data['toemail'])
        regexp = r"\A[^(]+\(\s*([^)]+?)\s*\)\Z"
        cc_users = []
        for line in data['carboncopy'].splitlines():
            match = re.search(regexp, line)
            if match:
                cc_users.append(match.group(1))
        header['Cc'] = ';'.join(cc_users)
        header['Reply-To'] = header['From']

        return header

    # def format_html(self, data):

    #     chrome = Chrome(self.env)
    #     dirs = []
    #     for provider in chrome.template_providers:
    #         dirs += provider.get_templates_dirs()
    #     templates = TemplateLoader(dirs, variable_lookup='lenient')

    #     _buffer = StringIO()
    #     try:
    #         template = templates.load('ecm_sending_email.html', cls=MarkupTemplate)
    #         if template:
    #             stream = template.generate(**data)
    #             stream.render('xhtml', doctype=DocType.XHTML_STRICT, out=_buffer)
    #     except TemplateNotFound:
    #         pass

    #     return _buffer.getvalue()
    def format_html(self, data):
        chrome = Chrome(self.env)
        dirs = []
        for provider in chrome.template_providers:
            dirs += provider.get_templates_dirs()
        env = Environment(loader=FileSystemLoader(dirs), trim_blocks=True)

        _buffer = StringIO()
        try:
            template = env.get_template('ecm_sending_email.html')
            if template:
                _buffer.write(template.render(**data))
        except TemplateNotFound:
            pass

        return _buffer.getvalue()

    def format_img(self, data):
        data_img = urllib.request.urlopen('%s/htdocs/%s' % (data['host_url'], data['image'])).read()
        return data_img

    def format_text(self, data):
        text = (u"*** This email was generated automatically by Trac on behalf of %s (Ticket #%s) ***\n"
                u"*** This is email #%s out of a total of %s email(s) ***\n\n"
                u"Please find attached the following %s:\n"
                u"%s\n\n"
                u"%s:\n"
                u"%s\n\n") % (
                    data['sendername'],
                    data['ticket_id'],
                    data['email_no'],
                    data['emails_count'],
                    data['attachment_introduction'],
                    data['attachment_name'],
                    data['attachment_content_type'],
                    '\n'.join(data['attachment_content']))

        if data['attachment_comment']:
            text += (u"Comment:\n"
                     u"%s\n\n") % data['attachment_comment']

        text += (u"Regards\n\n"
                 u"%s\n"
                 u"%s\n\n"
                 u"%s\n"
                 u"%s\n"
                 u"%s\n"
                 u"%s\n"
                 u"%s\n"
                 u"%s\n\n"
                 u"Tel: %s\n"
                 u"%s\n\n"
                 u"%s\n\n"
                 u"Please consider the environment before printing this e-mail.\n\n"
                 ) % (
                     data['sendername'],
                     data['fromrole'],
                     data['productgroup'],
                     data['division'],
                     data['address1'],
                     data['address2'],
                     data['address3'],
                     data['country'],
                     data['fromphone'],
                     data['senderemail'],
                     data['company_website'])

        return text

    def get_activities(self, **kwargs):
        """ Get ticket current activity """
        activities = {}
        if Ticket_UI.get_UI(self.ticket).legacy:
            activities['01-assigned_for_description'] = _("Description Action")
            activities['07-assigned_for_closure_actions'] = _("Closure Actions")
            activities['closed'] = _("None")
        else:
            activities['01-assigned_for_edition'] = _("Edition")
            activities['02-assigned_for_review'] = _("Piloting of Review")
            activities['03-assigned_for_approval'] = _("Approval")
            activities['04-approved'] = _("Piloting of Approval")
            activities['05-assigned_for_sending'] = _("Sending")
            activities['closed'] = _("None")

        return activities

    def get_approver(self):
        if self.ticket['status'] == '03-assigned_for_approval':
            approver = self.ticket['owner']
        else:
            approver = None

        return approver

    def get_approvers(self):
        approvers = []
        changes = [change for change in
                   self.component.ticket_module.rendered_changelog_entries(self.req, self.ticket)]
        for change in reversed(changes):
            if ('status' in change['fields'] and
                change['fields']['status']['old'] in ('01-assigned_for_edition', '02-assigned_for_review') and
                change['fields']['status']['new'] == '03-assigned_for_approval'):
                break
            elif ('signed as approver by' in change['comment']):
                regexp = r"[\s]+([a-z-]+\.[a-z-]+)[\s(]?"
                match = re.search(regexp, change['comment'])
                if match:
                    approver_name = match.group(1)
                else:
                    continue
                approver_roles = self.get_roles_by_initials(approver_name)
                regexp = '(%s)' % '|'.join(list(approver_roles.keys()))
                match = re.search(regexp, change['comment'])
                if match:
                    approver_role = match.group(1)
                else:
                    approver_role = '(role not set)'
                approvers.append('%s %s' % (approver_role, approver_name))

        return approvers

    def get_author(self):
        """
        Get the author
        """
        if self.ticket['status'] in ('03-assigned_for_approval', '05-assigned_for_sending'):
            author = self.get_peer()
        else:
            author = self.ticket['owner']

        return author

    @staticmethod
    def get_authors(ticket):
        """The authors are determined based on the ticket history of changes:
           tracking of source_url changes (which imply commits of ECM/FEE record)

        They include:
          * updating the source_url when submitting to set new custom xml data
          * updating the source_url when unlocking to update source and pdf files
        """
        ticket_module = TicketModule(ticket.env)
        author_regexp = _('^Source Url changed \(on behalf of (.+)\)$')
        authors = OrderedSet()

        for change in ticket_module.grouped_changelog_entries(ticket):
            match = re.search(author_regexp, change['comment'])
            if 'sourceurl' in change['fields'] and match:
                authors.add(match.group(1))

        return authors

    def get_certificate_validity_required(self):
        return self.component.certificate_validity_required

    def get_new_status(self):
        """Determines the new status"""
        action = self.action if self.action else 'reassign'
        status = self.get_status(self.ticket)
        new_status = self.component.actions[action]['newstate']

        if new_status == '*':
            triage_field = self.component.actions[action]['triage_field']
            new_status = self.component.actions[action]['triage_status'][self.get_triage_value(triage_field)][status]
            if new_status == '*':
                if action == 'reopen':
                    new_status = self.get_previous_status()
                elif action in ('abort_optional_approval', 'abort_sending'):
                    new_status = self.get_previous_status(force_backwards=True)
                else:
                    # 'view' or 'reassign' actions only: status does not change
                    new_status = status

        return new_status

    def get_owner_hint(self, operation, post_id='126'):
        return super(ECM2WF, self).get_owner_hint(operation, post_id)

    def get_projmgr(self):
        """
        Get the Project Manager
        """
        if self.ticket['status'] == '05-assigned_for_sending':
            projmgr = self.ticket['owner']
        else:
            projmgr = None

        return projmgr

    def get_reassign_labels(self):
        reassign_labels = {}

        if Ticket_UI.get_UI(self.ticket).legacy:
            reassign_labels['01-assigned_for_description'] = "reassign for description"
            reassign_labels['07-assigned_for_closure_actions'] = "reassign for closure actions"
        else:
            reassign_labels['01-assigned_for_edition'] = "reassign for edition"
            reassign_labels['02-assigned_for_review'] = "reassign for piloting of review"
            reassign_labels['03-assigned_for_approval'] = "reassign for (optional) approval"
            reassign_labels['04-approved'] = "reassign for piloting of approval"
            reassign_labels['05-assigned_for_sending'] = "reassign for sending"

        return reassign_labels

    def get_reviewed_tag(self, action=None):
        action = self.action if action is None else action
        if action == 'send':
            reviewed_tag = self.get_previous_tag(self.ticket['document'])
        else:
            reviewed_tag = self.ticket['document']

        return reviewed_tag

    def get_reviewers_count(self):
        # Number of reviewers that agreed to sign (based on PRF)
        reviewers_count = len(self.get_signers_to_sign('assign_for_optional_approval',
                                                       '03-assigned_for_approval',
                                                       'apply_reviewers_signatures'))
        return reviewers_count

    def get_signature_comment(self, doc, signatures):
        return ('%s signed as %s' %
                (self.ticket['pdffile'],
                 'and '.join([_f for _f in self.get_signed_as(signatures) if _f])))

    def get_signature_timestamp(self):
        return self.component.released_signature_timestamp

    def get_triage_value(self, triage_field):
        return '%s2' % self.ticket[triage_field]

    def get_version_status(self, action=None):
        """Get new tag version status"""
        action = self.action if action is None else action
        if self.ticket['ecmtype'] == 'Technical Note':
            tagged_item = self.ticket['summary']
            if action == 'assign_for_optional_review':
                indexes = [tg.status_index
                           for tg in Tag.select(
                               self.env,
                               ["tagged_item='%s'" % tagged_item],
                               ordering_term='status_index',
                               tag_type='version_tags')
                           if tg.status_index is not None]
                if indexes:
                    index = indexes[-1] + 1
                else:
                    index = 1
                version_status = '%02d' % index
            else:
                version_status = ''
        else:
            version_status = None

        return version_status

    def is_action_allowed_core(self, action_allowed, action=None, **kwargs):
        action = self.action if action is None else action

        action_allowed = super(ECM2WF, self).is_action_allowed_core(action_allowed, action)

        # No actions are allowed if the ECM/FEE ticket is in Edition mode
        if self.ticket['status'] == '01-assigned_for_edition':
            template_cls = cache.Ticket_Cache.get_subclass(self.ticket['type'])
            with template_cls(self.env,
                              self.component.trac_env_name,
                              self.req.authname,
                              self.ticket) as doc:
                repos_status = doc.status(self.ticket['sourcefile'], 'repos-status')
                if repos_status['lock_agent'] == 'trac':
                    action_allowed = (False, "No action is allowed as long as the source file is locked")
                    return action_allowed
                repos_status = doc.status(self.ticket['pdffile'], 'repos-status')
                if repos_status['lock_agent'] == 'trac':
                    action_allowed = (False, "No action is allowed as long as the pdf file is locked")
                    return action_allowed

        force_reassign = kwargs.get('force_reassign', None)
        if self.ticket['status'] == '01-assigned_for_edition' and action != 'reassign' and force_reassign == 'True':
            action_allowed = (False, "Force reassign so as to make fields with modified values make their way into the PDF before leaving the edition status and signing that PDF")

        elif self.ticket['status'] == '02-assigned_for_review' and action in ('assign_for_optional_approval', 'assign_for_sending'):
            childtickets = util.child_tickets_for_tag(self.ticket)
            if not childtickets:
                action_allowed = (False, "There are no child tickets for current version status")
            elif not [tkt['signer'] for tkt in childtickets
                      if tkt['signer'] and tkt['resolution'] == 'fixed']:
                action_allowed = (False, "No child PRF for that version status were closed as fixed with no remarks to implement")

        return action_allowed

    def is_action_filtered(self):
        if self.ticket['ecmtype'] == 'Document Delivery':
            if self.action == 'assign_for_optional_review':
                return True
            else:
                return False
        else:
            return False

    def send_email(self, data):
        """Send a MIMEMultipart message."""

        msg = MIMEMultipart()

        # email body
        html = self.format_html(data)
        if html:
            part = MIMEText(html, 'html', 'utf-8')
        else:
            text = self.format_text(data)
            part = MIMEText(text, 'plain', 'utf-8')
        msg.attach(part)

        # email embedded image
        if html:
            part = MIMEImage(self.format_img(data))
            part.add_header('Content-ID', '<{}>'.format(data['image']))
            msg.attach(part)

        # email header
        header = self.format_header(data)
        for key in list(header.keys()):
            msg[key] = header[key]

        # email attachment
        part = MIMEBase('application', "octet-stream")
        attachment = open(data['attachment_pathname'], "rb")
        part.set_payload(attachment.read())
        Encoders.encode_base64(part)
        part.add_header('Content-Disposition', 'attachment; filename="%s"' % data['attachment_name'])
        msg.attach(part)

        # email subject
        msg['Subject'] = data['subject']

        # Send email
        s = smtplib.SMTP('localhost')
        s.sendmail(msg['From'], [msg['To'], data['fromemail'], data['senderemail']] + msg['Cc'].split(';'), msg.as_string())
        s.quit

    def send_emails(self):
        """Send one email with ECM/FEE attached and optionally one or more emails with one attachment attached to each."""

        if self.action == 'send':

            send_resolution = self.env.config.get('ticket-workflow', 'send.triage_set_resolution').split(' -> ')[1].strip()
            if send_resolution == 'sent':

                roles = self.get_roles_by_initials(self.req.authname)
                role = self.req.args.get(self.action + '_set_role')

                hostname = util.get_hostname(self.env)
                scheme = self.env.config.get('artusplugin', 'scheme')
                host_url = '%s://%s' % (scheme, hostname)

                data = dict(
                    fromname=self.ticket['fromname'],
                    fromrole=roles[role] if roles else '(role not set)',
                    fromemail=self.ticket['fromemail'],
                    fromphone=self.ticket['fromphone'],
                    toname=self.ticket['toname'],
                    toemail=self.ticket['toemail'],
                    tophone=self.ticket['tophone'],
                    carboncopy=self.ticket['carboncopy'],
                    productgroup=self.env.config.get('artusplugin', 'productgroup'),
                    location=self.env.config.get('artusplugin', 'location'),
                    division=self.env.config.get('artusplugin', 'division'),
                    address1=self.env.config.get('artusplugin', 'address1'),
                    address2=self.env.config.get('artusplugin', 'address2'),
                    address3=self.env.config.get('artusplugin', 'address3'),
                    country=self.env.config.get('artusplugin', 'country'),
                    company_website=self.env.config.get('artusplugin', 'company_website'),
                    image='MEGGITT.png',
                    host_url=host_url,
                    ticket_id=self.ticket.id,
                    ticket_url='%s%s' % (self.req.base_url, self.req.path_info)
                )

                data['sendername'] = util.formatted_name(self.req.authname)
                with Ldap_Utilities() as ldap_util:
                    data['senderemail'] = util.Users.get_email(self.env, self.req.authname, ldap_util)

                # --------------- First email (ECM) ----------------------

                data['attachment_introduction'] = '%s Engineering Coordination Memo (ECM) written by %s' % (self.ticket['ecmtype'], self.ticket['fromname'])
                data['attachment_name'] = '%s' % self.ticket['pdffile']
                data['attachment_content_type'] = 'Subject'
                data['attachment_content'] = [self.ticket['keywords']]
                data['attachment_comment'] = self.req.args.get('comment', '(no comment)')

                template_cls = cache.Ticket_Cache.get_subclass(self.ticket['type'])
                with template_cls(self.env,
                                  self.component.trac_env_name,
                                  self.req.authname,
                                  self.ticket) as doc:
                    data['attachment_pathname'] = "%s/%s" % (doc.path, self.ticket['pdffile'])

                subject = "%s: %s" % (self.ticket['ecmtype'], self.ticket['summary'])
                emails_count = 1  # ECM
                if self.ticket['ecmtype'] == 'Technical Note':
                    emails_count += len([a for a in Attachment.select(self.env, 'ticket', self.ticket.id)])
                else:
                    emails_count += cache.PDFPackage.get_archives_number(self.ticket)
                if emails_count > 1:
                    data['email_no'] = 1
                    data['emails_count'] = emails_count
                    data['subject'] = '%s (%s/%s)' % (subject, data['email_no'], data['emails_count'])
                else:
                    data['email_no'] = 1
                    data['emails_count'] = 1
                    data['subject'] = subject

                self.send_email(data)

                # --------------- Remaining emails (attachments) ----------------------

                if self.ticket['ecmtype'] == 'Technical Note':
                    # Ticket attachments
                    for index, attachment in enumerate(Attachment.select(self.env, 'ticket', self.ticket.id), start=2):

                        data['attachment_introduction'] = 'attachment'
                        data['attachment_name'] = '%s' % attachment.filename
                        data['attachment_content_type'] = 'Description'
                        data['attachment_content'] = [attachment.description] if attachment.description else ['(no description)']
                        data['attachment_comment'] = None
                        data['attachment_pathname'] = attachment.path
                        data['email_no'] = index
                        data['emails_count'] = emails_count
                        data['subject'] = '%s (%s/%s)' % (subject, data['email_no'], data['emails_count'])

                        self.send_email(data)
                else:
                    # Zip archives
                    for index, archive_path in enumerate(cache.PDFPackage.get_archives_paths(self.ticket), start=2):

                        data['attachment_introduction'] = 'zip archive'
                        data['attachment_name'] = '%s' % os.path.basename(archive_path)
                        data['attachment_content_type'] = 'Zip content'
                        with ZipFile(archive_path, 'r') as archive:
                            data['attachment_content'] = [zipinfo.filename
                                                          for zipinfo in archive.infolist()
                                                          if not zipinfo.filename.endswith('/')]
                        data['attachment_comment'] = None
                        data['attachment_pathname'] = archive_path
                        data['email_no'] = index
                        data['emails_count'] = emails_count
                        data['subject'] = '%s (%s/%s)' % (subject, data['email_no'], data['emails_count'])

                        self.send_email(data)

    def set_tag(self):
        # tag for review
        if (self.ticket['ecmtype'] == 'Technical Note' and
            self.action in ('assign_for_optional_review', 'assign_for_sending')):
            db = self.env.get_db_cnx()
            panel = VersionTagsAdminPanel
            version_status = self.get_version_status()
            tag_data = {}
            if version_status:
                tag_data['tag_name'] = "%s.%s" % (self.ticket['summary'], version_status)
            else:
                tag_data['tag_name'] = "%s" % self.ticket['summary']
            tag_data['ci_name'] = self.ticket['configurationitem']
            tag_data['authname'] = self.req.authname
            tag_data['modification'] = None
            tag_data['amendment'] = None
            tag_data['standard'] = None
            tag_data['edition'] = None
            tag_data['revision'] = None
            tag_data['status'] = None
            tag_data['status_index'] = version_status
            tag_data['component'] = 'False'
            tag_data['source_url'] = self.ticket['sourceurl']
            tag_data['baselined'] = 'False'
            tag_data['buildbot'] = 'False'
            tag_data['version_type'] = None
            tag_data['from_tag'] = None
            tag_data['program_name'] = self.component.program_name
            tag_data['ticket_id'] = self.ticket.id
            panel.create_tag(self.env, self.req.href, panel.cat_type,
                             panel.page_type, tag_data, db)
            tag_data['comment'] = "tag %s" % tag_data['tag_name']
            tag_data['buildbot_progbase'] = "%s/%s" % (
                BuildBotModule.buildbot_projects,
                self.component.trac_env_name)
            tag_data['tag_url'] = panel.tag_url_from_source_url(
                self.env,
                tag_data['tag_name'])
            panel.apply_tag(self.env, self.req.href, panel.cat_type,
                            panel.page_type, tag_data, db)
            self.ticket['document'] = tag_data['tag_name']
            self.ticket['documenturl'] = tag_data['tag_url']
            # Update description field with progress meter for the new status
            if self.action == 'assign_for_optional_review':
                self.ticket['description'] = ('%s:[[TicketQuery(parent=#%s, '
                                              'document=%s, format=progress)]]' % (
                                                  tag_data['tag_name'],
                                                  self.ticket.id,
                                                  tag_data['tag_name']))
            now = datetime.now(utc)
            self.ticket.save_changes('trac',
                                     _('Tag %(tag)s applied', tag=tag_data['tag_name']), now)

    def unset_tag(self):
        # remove tag
        if (self.ticket['ecmtype'] == 'Technical Note' and
            self.ticket['document']):
            tg = Tag(self.env, self.ticket['document'])
            if (self.action == 'abort_sending' and
                tg.status_index is None):
                db = self.env.get_db_cnx()
                tag_data = {}
                tag_data['tag_name'] = self.ticket['document']
                tag_data['authname'] = self.req.authname
                VersionTagsAdminPanel.remove_tag(self.env, self.req.href, 'tags_mgmt',
                                                 'version_tags', tag_data, db)
                previous_tag = self.get_previous_tag(tag_data['tag_name'])
                if previous_tag:
                    tg = Tag(self.env, previous_tag)
                    self.ticket['document'] = previous_tag
                    self.ticket['documenturl'] = tg.tag_url
                    self.ticket['description'] = ('%s:[[TicketQuery(parent=#%s, '
                                                  'document=%s, format=progress)]]' % (
                                                      previous_tag,
                                                      self.ticket.id,
                                                      previous_tag))
                else:
                    self.ticket['document'] = ''
                    self.ticket['documenturl'] = ''
                    self.ticket['description'] = ''
                now = datetime.now(utc)
                self.ticket.save_changes('trac',
                                         _('Tag %(tag)s removed', tag=tag_data['tag_name']), now)

    def update_document(self):
        # Update document data
        if (self.ticket['status'] == '01-assigned_for_edition' and
            self.ticket['sourcefile'].endswith('.docm')):
            template_cls = cache.Ticket_Cache.get_subclass(self.ticket['type'])
            with template_cls(self.env,
                              self.component.trac_env_name,
                              self.req.authname,
                              self.ticket) as doc:

                doc.checkout(self.ticket['sourcefile'])
                doc.lock(self.ticket['sourcefile'])
                doc.update_data(self.ticket['sourcefile'], True)

                # Commit changes
                revision = doc.commit()
                if revision != '':
                    # Used for author tracking
                    self.ticket['sourceurl'] = '%s?rev=%s' % (
                        util.get_url(self.ticket['sourceurl']),
                        revision)
                    now = datetime.now(utc)
                    self.ticket.save_changes('trac',
                                             _('Source Url changed (on behalf of %(user)s)', user=self.req.authname),
                                             now)
                else:
                    # Remove the lock
                    doc.unlock(self.ticket['sourcefile'])

    def with_independence(self):
        with_independence = False
        if self.action in ('assign_for_optional_approval',
                           'reassign_for_optional_approval',
                           'abort_optional_approval',
                           'optional_approve'):
            with_independence = True

        return with_independence

class FEEWF(DocWF):

    @staticmethod
    def max_reviewers(ticket):
        # The number of reviewers that can sign depends on:
        #       - the signatures reserved internally:
        #           - author
        #           - sender
        #           - trade compliance if not delegated to the sender
        tc_approval_required = ticket.env.config.getbool('artusplugin', 'tc_approval_required', False)
        tc_approval_delegated = ticket.env.config.getbool('artusplugin', 'tc_approval_delegated', False)
        available_signature_boxes = 6
        reserved_signature_boxes = 3 if (tc_approval_required and not tc_approval_delegated) else 2
        max_reviewers = available_signature_boxes - reserved_signature_boxes

        return max_reviewers

    @staticmethod
    def max_reviewers_reached(ticket, childtickets):
        max_reviewers_reached = False

        # Max number of reviewers always managed
        max_reviewers = FEEWF.max_reviewers(ticket)
        current_reviewers = len([ct for ct in childtickets if ct['type'] == 'PRF' and ct['document'] == ticket['document']
                                 and (ct['status'] != 'closed' or ct['resolution'] != 'rejected')])
        if current_reviewers == max_reviewers:
            max_reviewers_reached = True

        return max_reviewers_reached

    @staticmethod
    def max_reviewers_reached_message(ticket):
        tc_approval_required = ticket.env.config.getbool('artusplugin', 'tc_approval_required', False)
        tc_approval_delegated = ticket.env.config.getbool('artusplugin', 'tc_approval_delegated', False)
        if tc_approval_required and not tc_approval_delegated:
            message = 'You cannot create an additional PRF because two signatures must remain available (trade compliance and sending)'
        elif not tc_approval_required or tc_approval_delegated:
            message = 'You cannot create an additional PRF because one signature must remain available (sending)'

        return message

    @staticmethod
    def too_many_reviewers(ticket, childtickets):
        too_many_reviewers = False

        # Max number of reviewers outgrown
        max_reviewers = FEEWF.max_reviewers(ticket)
        current_reviewers = len([ct for ct in childtickets if ct['type'] == 'PRF' and ct['document'] == ticket['document']
                                 and (ct['status'] != 'closed' or ct['resolution'] != 'rejected')])
        if current_reviewers > max_reviewers:
            too_many_reviewers = True

        return too_many_reviewers

    @staticmethod
    def too_many_reviewers_message(ticket):
        tc_approval_required = ticket.env.config.getbool('artusplugin', 'tc_approval_required', False)
        tc_approval_delegated = ticket.env.config.getbool('artusplugin', 'tc_approval_delegated', False)
        if tc_approval_required and not tc_approval_delegated:
            message = 'Too many PRF have been created, you must delete or reject some of them because two signatures must remain available (trade compliance and sending)'
        elif not tc_approval_required or tc_approval_delegated:
            message = 'Too many PRF have been created, you must delete or reject some of them because one signature must remain available (sending)'

        return message

    @staticmethod
    def childticket_creation_message(ticket, childtickets):
        if FEEWF.max_reviewers_reached(ticket, childtickets):
            return FEEWF.max_reviewers_reached_message(ticket)
        elif FEEWF.too_many_reviewers(ticket, childtickets):
            return FEEWF.too_many_reviewers_message(ticket)
        else:
            return ''

    def format_header(self, data):

        header = {}
        header['Sender'] = '"%s" <%s>' % (data['sendername'], data['senderemail'])
        header['From'] = header['Sender']
        header['To'] = '"%s" <%s>' % (data['toname'], data['toemail'])
        regexp = r"\A[^(]+\(\s*([^)]+?)\s*\)\Z"
        cc_users = []
        for line in data['carboncopy'].splitlines():
            match = re.search(regexp, line)
            if match:
                cc_users.append(match.group(1))
        header['Cc'] = ';'.join(cc_users)
        header['Reply-To'] = header['From']

        return header

    def format_html(self, data):

        chrome = Chrome(self.env)
        dirs = []
        for provider in chrome.template_providers:
            dirs += provider.get_templates_dirs()
        templates = TemplateLoader(dirs, variable_lookup='lenient')

        _buffer = StringIO()
        try:
            template = templates.load('ecm_sending_email.html', cls=MarkupTemplate)
            if template:
                stream = template.generate(**data)
                stream.render('xhtml', doctype=DocType.XHTML_STRICT, out=_buffer)
        except TemplateNotFound:
            pass

        return _buffer.getvalue()

    def format_img(self, data):
        data_img = urllib.request.urlopen('%s/htdocs/%s' % (data['host_url'], data['image'])).read()
        return data_img

    def format_text(self, data):
        text = (u"*** This email was generated automatically by Trac on behalf of %s (Ticket #%s) ***\n"
                u"*** This is email #%s out of a total of %s email(s) ***\n\n"
                u"Please find attached the following %s:\n"
                u"%s\n\n"
                u"%s:\n"
                u"%s\n\n") % (
                    data['sendername'],
                    data['ticket_id'],
                    data['email_no'],
                    data['emails_count'],
                    data['attachment_introduction'],
                    data['attachment_name'],
                    data['attachment_content_type'],
                    '\n'.join(data['attachment_content']))

        if data['attachment_comment']:
            text += (u"Comment:\n"
                     u"%s\n\n") % data['attachment_comment']

        text += (u"Regards\n\n"
                 u"%s\n"
                 u"%s\n\n"
                 u"%s\n"
                 u"%s\n"
                 u"%s\n"
                 u"%s\n"
                 u"%s\n"
                 u"%s\n\n"
                 u"Tel: %s\n"
                 u"%s\n\n"
                 u"%s\n\n"
                 u"Please consider the environment before printing this e-mail.\n\n"
                 ) % (
                     data['sendername'],
                     data['fromrole'],
                     data['productgroup'],
                     data['division'],
                     data['address1'],
                     data['address2'],
                     data['address3'],
                     data['country'],
                     data['fromphone'],
                     data['senderemail'],
                     data['company_website'])

        return text

    def get_activities(self, **kwargs):
        """ Get ticket current activity """
        activities = {}
        activities['01-assigned_for_edition'] = _("Edition")
        activities['02-assigned_for_review_management'] = _("Review Management")
        activities['03-assigned_for_internal_approval_management'] = _("Internal Approval Management")
        activities['04-assigned_for_approval'] = _("Approval")
        activities['05-assigned_for_customer_approval_management'] = _("Customer Approval Management")
        activities['06-assigned_for_closure_actions'] = _("Closure Actions")
        activities['closed'] = _("None")

        return activities

    def get_approver(self):
        if self.ticket['status'] == '04-assigned_for_approval':
            approver = self.ticket['owner']
        else:
            approver = None

        return approver

    def get_approvers(self):
        approvers = []
        changes = [change for change in
                   self.component.ticket_module.rendered_changelog_entries(self.req, self.ticket)]
        for change in reversed(changes):
            if ('status' in change['fields'] and
                change['fields']['status']['old'] == '02-assigned_for_review_management' and
                change['fields']['status']['new'] == '03-assigned_for_internal_approval_management'):
                break
            elif ('signed as approver by' in change['comment']):
                regexp = r"[\s]+([a-z-]+\.[a-z-]+)[\s(]?"
                match = re.search(regexp, change['comment'])
                if match:
                    approver_name = match.group(1)
                else:
                    continue
                approver_roles = self.get_roles_by_initials(approver_name)
                regexp = '(%s)' % '|'.join(list(approver_roles.keys()))
                match = re.search(regexp, change['comment'])
                if match:
                    approver_role = match.group(1)
                else:
                    approver_role = '(role not set)'
                approvers.append('%s %s' % (approver_role, approver_name))

        return approvers

    def get_author(self):
        """
        Get the author
        """
        if self.ticket['status'] == '01-assigned_for_edition':
            author = self.ticket['owner']
        else:
            author = None

        return author

    @staticmethod
    def get_authors(ticket):
        """The authors are determined based on the ticket history of changes:
           tracking of source_url changes (which imply commits of FEE record)

        They include:
          * updating the source_url when submitting to set new custom xml data
          * updating the source_url when unlocking to update source and pdf files
        """
        ticket_module = TicketModule(ticket.env)
        author_regexp = _('^Source Url changed \(on behalf of (.+)\)$')
        authors = OrderedSet()

        for change in ticket_module.grouped_changelog_entries(ticket):
            match = re.search(author_regexp, change['comment'])
            if 'sourceurl' in change['fields'] and match:
                authors.add(match.group(1))

        return authors

    def get_certificate_validity_required(self):
        return self.component.certificate_validity_required

    def get_new_status(self):
        """Determines the new status"""
        action = self.action if self.action else 'reassign'
        status = self.get_status(self.ticket)
        new_status = self.component.actions[action]['newstate']

        if new_status == '*':
            triage_field = self.component.actions[action]['triage_field']
            new_status = self.component.actions[action]['triage_status'][self.get_triage_value(triage_field)][status]
            if new_status == '*':
                if action == 'reopen':
                    new_status = self.get_previous_status()
                else:
                    # 'view' or 'reassign' actions only: status does not change
                    new_status = status

        return new_status

    def get_owner_hint(self, operation, post_id='158'):
        return super(FEEWF, self).get_owner_hint(operation, post_id)

    def get_reassign_labels(self):
        reassign_labels = {}

        reassign_labels['01-assigned_for_edition'] = _("reassign for edition")
        reassign_labels['02-assigned_for_review_management'] = _("reassign for review management")
        reassign_labels['03-assigned_for_internal_approval_management'] = _("reassign for internal approval management")
        reassign_labels['04-assigned_for_approval'] = _("reassign for internal approval management")
        reassign_labels['05-assigned_for_customer_approval_management'] = _("reassign for customer approval management")
        reassign_labels['06-assigned_for_closure_actions'] = _("reassign for closure actions")

        return reassign_labels

    def get_reviewed_tag(self, action=None):
        action = self.action if action is None else action
        reviewed_tag = self.ticket['document']

        return reviewed_tag
    
    def get_reviewers_count(self):
        # Number of reviewers that agreed to sign (based on PRF)
        reviewers_count = len(self.get_signers_to_sign('assign_for_fee_internal_approval_management',
                                                       '03-assigned_for_internal_approval_management',
                                                       'apply_reviewers_signatures'))
        return reviewers_count

    def get_signature_comment(self, doc, signatures):
        return ('%s signed as %s' %
                (self.ticket['pdffile'],
                 'and '.join([_f for _f in self.get_signed_as(signatures) if _f])))

    def get_signature_timestamp(self):
        return self.component.released_signature_timestamp

    def get_version_status(self, action=None):
        """Get new tag version status"""
        action = self.action if action is None else action
        tagged_item = self.ticket['summary']
        if action == 'assign_for_fee_review_management':
            indexes = [tg.status_index
                        for tg in Tag.select(
                            self.env,
                            ["tagged_item='%s'" % tagged_item],
                            ordering_term='status_index',
                            tag_type='version_tags')
                        if tg.status_index is not None]
            if indexes:
                index = indexes[-1] + 1
            else:
                index = 1
            version_status = '%02d' % index
        else:
            version_status = ''

        return version_status

    def is_action_allowed_core(self, action_allowed, action=None, **kwargs):
        action = self.action if action is None else action

        action_allowed = super(FEEWF, self).is_action_allowed_core(action_allowed, action)

        # No actions are allowed if the FEE ticket is in Edition mode
        if self.ticket['status'] == '01-assigned_for_edition':
            template_cls = cache.Ticket_Cache.get_subclass(self.ticket['type'])
            with template_cls(self.env,
                              self.component.trac_env_name,
                              self.req.authname,
                              self.ticket) as doc:
                repos_status = doc.status(self.ticket['sourcefile'], 'repos-status')
                if repos_status['lock_agent'] == 'trac':
                    action_allowed = (False, "No action is allowed as long as the source file is locked")
                    return action_allowed
                repos_status = doc.status(self.ticket['pdffile'], 'repos-status')
                if repos_status['lock_agent'] == 'trac':
                    action_allowed = (False, "No action is allowed as long as the pdf file is locked")
                    return action_allowed

        force_reassign = kwargs.get('force_reassign', None)
        if self.ticket['status'] == '01-assigned_for_edition' and action != 'reassign' and force_reassign == 'True':
            action_allowed = (False, "Force reassign so as to make fields with modified values make their way into the PDF before leaving the edition status and signing that PDF")

        elif self.ticket['status'] == '02-assigned_for_review_management' and action == 'assign_for_fee_internal_approval_management':
            childtickets = util.child_tickets_for_tag(self.ticket)
            if not childtickets:
                action_allowed = (False, "There are no child tickets for current version status")
            elif not [tkt['signer'] for tkt in childtickets
                      if tkt['signer'] and tkt['resolution'] == 'fixed']:
                action_allowed = (False, "No child PRF for that version status were closed as fixed with no remarks to implement")

        return action_allowed

    def is_action_filtered(self):
        return False

    def send_email(self, data):
        """Send a MIMEMultipart message."""

        msg = MIMEMultipart()

        # email body
        html = self.format_html(data)
        if html:
            part = MIMEText(html, 'html', 'utf-8')
        else:
            text = self.format_text(data)
            part = MIMEText(text, 'plain', 'utf-8')
        msg.attach(part)

        # email embedded image
        if html:
            part = MIMEImage(self.format_img(data))
            part.add_header('Content-ID', '<{}>'.format(data['image']))
            msg.attach(part)

        # email header
        header = self.format_header(data)
        for key in list(header.keys()):
            msg[key] = header[key]

        # email attachment
        part = MIMEBase('application', "octet-stream")
        attachment = open(data['attachment_pathname'], "rb")
        part.set_payload(attachment.read())
        Encoders.encode_base64(part)
        part.add_header('Content-Disposition', 'attachment; filename="%s"' % data['attachment_name'])
        msg.attach(part)

        # email subject
        msg['Subject'] = data['subject']

        # Send email
        s = smtplib.SMTP('localhost')
        s.sendmail(msg['From'], [msg['To'], data['fromemail'], data['senderemail']] + msg['Cc'].split(';'), msg.as_string())
        s.quit

    def send_emails(self):
        """Send one email with ECM/FEE attached and optionally one or more emails with one attachment attached to each."""

        if self.action == 'assign_for_customer_approval_management':

            roles = self.get_roles_by_initials(self.req.authname)
            role = self.req.args.get(self.action + '_set_role')

            hostname = util.get_hostname(self.env)
            scheme = self.env.config.get('artusplugin', 'scheme')
            host_url = '%s://%s' % (scheme, hostname)

            data = dict(
                fromname=self.ticket['fromname'],
                fromrole=roles[role] if roles else '(role not set)',
                fromemail=self.ticket['fromemail'],
                fromphone=self.ticket['fromphone'],
                toname=self.ticket['toname'],
                toemail=self.ticket['toemail'],
                tophone=self.ticket['tophone'],
                carboncopy=self.ticket['carboncopy'],
                productgroup=self.env.config.get('artusplugin', 'productgroup'),
                location=self.env.config.get('artusplugin', 'location'),
                division=self.env.config.get('artusplugin', 'division'),
                address1=self.env.config.get('artusplugin', 'address1'),
                address2=self.env.config.get('artusplugin', 'address2'),
                address3=self.env.config.get('artusplugin', 'address3'),
                country=self.env.config.get('artusplugin', 'country'),
                company_website=self.env.config.get('artusplugin', 'company_website'),
                image='MEGGITT.png',
                host_url=host_url,
                ticket_id=self.ticket.id,
                ticket_url='%s%s' % (self.req.base_url, self.req.path_info)
            )

            data['sendername'] = util.formatted_name(self.req.authname)
            with Ldap_Utilities() as ldap_util:
                data['senderemail'] = util.Users.get_email(self.env, self.req.authname, ldap_util)

            # --------------- First email (FEE) ----------------------

            data['attachment_introduction'] = 'Equipment Evolution Sheet (FEE) written by %s' % self.ticket['fromname']                
            data['attachment_name'] = '%s' % self.ticket['pdffile']
            data['attachment_content_type'] = None
            data['attachment_content'] = ''
            data['attachment_comment'] = self.req.args.get('comment', '(no comment)')

            template_cls = cache.Ticket_Cache.get_subclass(self.ticket['type'])
            with template_cls(self.env,
                                self.component.trac_env_name,
                                self.req.authname,
                                self.ticket) as doc:
                data['attachment_pathname'] = "%s/%s" % (doc.path, self.ticket['pdffile'])

            subject = self.ticket['summary']
            emails_count = 1  # FEE
            emails_count += len([a for a in Attachment.select(self.env, 'ticket', self.ticket.id)])

            if emails_count > 1:
                data['email_no'] = 1
                data['emails_count'] = emails_count
                data['subject'] = '%s (%s/%s)' % (subject, data['email_no'], data['emails_count'])
            else:
                data['email_no'] = 1
                data['emails_count'] = 1
                data['subject'] = subject

            self.send_email(data)

            # --------------- Remaining emails (attachments) ----------------------

            # Ticket attachments
            for index, attachment in enumerate(Attachment.select(self.env, 'ticket', self.ticket.id), start=2):

                data['attachment_introduction'] = 'attachment'
                data['attachment_name'] = '%s' % attachment.filename
                data['attachment_content_type'] = 'Description'
                data['attachment_content'] = [attachment.description] if attachment.description else ['(no description)']
                data['attachment_comment'] = None
                data['attachment_pathname'] = attachment.path
                data['email_no'] = index
                data['emails_count'] = emails_count
                data['subject'] = '%s (%s/%s)' % (subject, data['email_no'], data['emails_count'])

                self.send_email(data)

    def set_tag(self):
        # tag for review
        if self.action in ('assign_for_fee_review_management', 'assign_for_fee_customer_approval_management'):
            db = self.env.get_db_cnx()
            panel = VersionTagsAdminPanel
            version_status = self.get_version_status()
            tag_data = {}
            if version_status:
                tag_data['tag_name'] = "%s.%s" % (self.ticket['summary'], version_status)
            else:
                tag_data['tag_name'] = "%s" % self.ticket['summary']
            tag_data['ci_name'] = self.ticket['configurationitem']
            tag_data['authname'] = self.req.authname
            tag_data['modification'] = None
            tag_data['amendment'] = None
            tag_data['standard'] = None
            tag_data['edition'] = None
            tag_data['revision'] = None
            tag_data['status'] = None
            tag_data['status_index'] = version_status
            tag_data['component'] = 'False'
            tag_data['source_url'] = self.ticket['sourceurl']
            tag_data['baselined'] = 'False'
            tag_data['buildbot'] = 'False'
            tag_data['version_type'] = None
            tag_data['from_tag'] = None
            tag_data['program_name'] = self.component.program_name
            tag_data['ticket_id'] = self.ticket.id
            panel.create_tag(self.env, self.req.href, panel.cat_type,
                             panel.page_type, tag_data, db)
            tag_data['comment'] = "tag %s" % tag_data['tag_name']
            tag_data['buildbot_progbase'] = "%s/%s" % (
                BuildBotModule.buildbot_projects,
                self.component.trac_env_name)
            tag_data['tag_url'] = panel.tag_url_from_source_url(
                self.env,
                tag_data['tag_name'])
            panel.apply_tag(self.env, self.req.href, panel.cat_type,
                            panel.page_type, tag_data, db)
            self.ticket['document'] = tag_data['tag_name']
            self.ticket['documenturl'] = tag_data['tag_url']
            # Update description field with progress meter for the new status
            if self.action == 'assign_for_fee_review_management':
                self.ticket['description'] = ('%s:[[TicketQuery(parent=#%s, '
                                              'document=%s, format=progress)]]' % (
                                                  tag_data['tag_name'],
                                                  self.ticket.id,
                                                  tag_data['tag_name']))
            now = datetime.now(utc)
            self.ticket.save_changes('trac',
                                     _('Tag %(tag)s applied', tag=tag_data['tag_name']), now)

    def unset_tag(self):
        # remove tag
        if self.ticket['document']:
            tg = Tag(self.env, self.ticket['document'])
            if (self.action == 'abort_fee_customer_approval_management' and
                tg.status_index is None):
                db = self.env.get_db_cnx()
                tag_data = {}
                tag_data['tag_name'] = self.ticket['document']
                tag_data['authname'] = self.req.authname
                VersionTagsAdminPanel.remove_tag(self.env, self.req.href, 'tags_mgmt',
                                                 'version_tags', tag_data, db)
                previous_tag = self.get_previous_tag(tag_data['tag_name'])
                if previous_tag:
                    tg = Tag(self.env, previous_tag)
                    self.ticket['document'] = previous_tag
                    self.ticket['documenturl'] = tg.tag_url
                    self.ticket['description'] = ('%s:[[TicketQuery(parent=#%s, '
                                                  'document=%s, format=progress)]]' % (
                                                      previous_tag,
                                                      self.ticket.id,
                                                      previous_tag))
                else:
                    self.ticket['document'] = ''
                    self.ticket['documenturl'] = ''
                    self.ticket['description'] = ''
                now = datetime.now(utc)
                self.ticket.save_changes('trac',
                                         _('Tag %(tag)s removed', tag=tag_data['tag_name']), now)

    def update_document(self):
        # Update document data
        if (self.ticket['status'] == '01-assigned_for_edition' and
            self.ticket['sourcefile'].endswith('.docm')):
            template_cls = cache.Ticket_Cache.get_subclass(self.ticket['type'])
            with template_cls(self.env,
                              self.component.trac_env_name,
                              self.req.authname,
                              self.ticket) as doc:

                doc.checkout(self.ticket['sourcefile'])
                doc.lock(self.ticket['sourcefile'])
                doc.update_data(self.ticket['sourcefile'], True)

                # Commit changes
                revision = doc.commit()
                if revision != '':
                    # Used for author tracking
                    self.ticket['sourceurl'] = '%s?rev=%s' % (
                        util.get_url(self.ticket['sourceurl']),
                        revision)
                    now = datetime.now(utc)
                    self.ticket.save_changes('trac',
                                             _('Source Url changed (on behalf of %(user)s)', user=self.req.authname),
                                             now)
                else:
                    # Remove the lock
                    doc.unlock(self.ticket['sourcefile'])

    def with_independence(self):
        with_independence = False
        if self.action in ('assign_for_fee_approval',
                           'abort_fee_approval',
                           'approve_fee'):
            with_independence = True

        return with_independence

class DOCWF(DocWF):

    @staticmethod
    def max_reviewers(ticket):
        # The number of reviewers that can sign depends on:
        #   - the template of the document (some signature boxes may be reserved for customers)
        #   - the signatures reserved internally:
        #       - author
        #       - quality
        #       - trade compliance if not delegated to a reviewer
        tc_approval_required = ticket.env.config.getbool('artusplugin', 'tc_approval_required', False)
        tc_approval_delegated = ticket.env.config.getbool('artusplugin', 'tc_approval_delegated', False)
        source_types = util.get_prop_values(ticket.env, "source_types")
        available_signatures_boxes = source_types[ticket['sourcetype']].split('||')[7].strip()
        if available_signatures_boxes:
            available_signature_boxes = int(available_signatures_boxes)
            reserved_signature_boxes = 3 if (tc_approval_required and not tc_approval_delegated) else 2
            max_reviewers = available_signature_boxes - reserved_signature_boxes
        else:
            max_reviewers = None

        return max_reviewers

    @staticmethod
    def max_reviewers_reached(ticket, childtickets):
        max_reviewers_reached = False

        # Max number of reviewers managed only for Proposed status
        if Tag(ticket.env, ticket['document']).status == 'Proposed':
            max_reviewers = DOCWF.max_reviewers(ticket)
            if max_reviewers is not None:
                current_reviewers = len([ct for ct in childtickets if ct['type'] == 'PRF' and ct['document'] == ticket['document']
                                         and (ct['status'] != 'closed' or ct['resolution'] != 'rejected')])
                if current_reviewers == max_reviewers:
                    max_reviewers_reached = True

        return max_reviewers_reached

    @staticmethod
    def max_reviewers_reached_message(ticket):
        tc_approval_required = ticket.env.config.getbool('artusplugin', 'tc_approval_required', False)
        tc_approval_delegated = ticket.env.config.getbool('artusplugin', 'tc_approval_delegated', False)
        if tc_approval_required and not tc_approval_delegated:
            message = 'You cannot create an additional PRF because two signatures must remain available (approval by quality and trade compliance)'
        elif not tc_approval_required or tc_approval_delegated:
            message = 'You cannot create an additional PRF because one signature must remain available (approval by quality)'
        else:
            message = ''

        return message

    @staticmethod
    def too_many_reviewers(ticket, childtickets):
        too_many_reviewers = False

        # Max number of reviewers outgrown
        if Tag(ticket.env, ticket['document']).status == 'Proposed':
            max_reviewers = DOCWF.max_reviewers(ticket)
            if max_reviewers is not None:
                current_reviewers = len([ct for ct in childtickets if ct['type'] == 'PRF' and ct['document'] == ticket['document']
                                         and (ct['status'] != 'closed' or ct['resolution'] != 'rejected')])
                if current_reviewers > max_reviewers:
                    too_many_reviewers = True

        return too_many_reviewers

    @staticmethod
    def too_many_reviewers_message(ticket):
        tc_approval_required = ticket.env.config.getbool('artusplugin', 'tc_approval_required', False)
        tc_approval_delegated = ticket.env.config.getbool('artusplugin', 'tc_approval_delegated', False)
        if tc_approval_required and not tc_approval_delegated:
            message = 'Too many PRF have been created, you must delete or reject some of them because two signatures must remain available (approval by quality and trade compliance)'
        elif not tc_approval_required or tc_approval_delegated:
            message = 'Too many PRF have been created, you must delete or reject some of them because one signature must remain available (approval by quality)'
        else:
            message = ''

        return message

    @staticmethod
    def childticket_creation_message(ticket, childtickets):
        if DOCWF.max_reviewers_reached(ticket, childtickets):
            return DOCWF.max_reviewers_reached_message(ticket)
        elif DOCWF.too_many_reviewers(ticket, childtickets):
            return DOCWF.too_many_reviewers_message(ticket)
        else:
            return ''

    def get_activities(self, **kwargs):
        """ Get ticket current activity """
        activities = {}
        status = self.get_status(self.ticket)
        version_status = kwargs.get('version_status', None)

        if status == '01-assigned_for_edition':
            if self.ticket['sourcefile'] and self.ticket['sourcefile'].endswith('.docm'):
                last_rev_repo = util.get_last_path_rev_author(
                    self.env, util.get_url(self.ticket['sourceurl']))[2]
                if last_rev_repo == util.get_revision(self.ticket['sourceurl']):
                    template_cls = cache.Ticket_Cache.get_subclass(self.ticket['type'])
                    with template_cls(self.env,
                                      self.component.trac_env_name,
                                      self.req.authname,
                                      self.ticket) as doc:
                        version_status = doc.get_version_status(self.ticket['sourcefile'])
                        if not version_status:
                            repos_status = doc.status(self.ticket['sourcefile'], 'repos-status')
                            if self.req.authname == self.ticket['owner'] and repos_status['lock_agent'] == 'trac':
                                # Use case: source file changed by Trac admin through winscp
                                doc.update_data(self.ticket['sourcefile'])
                                doc.upgrade_document(self.ticket['sourcefile'])
                                version_status = doc.get_version_status(self.ticket['sourcefile'])
                                if not version_status:
                                    raise TracError("Version status is missing in document header", "Source file error", True)
                            else:
                                raise TracError("Version status is missing in document header", "Source file error", True)
            if (not version_status):
                version_status = 'Draft'
            if version_status.startswith('Draft'):
                doc_status = self.get_version_status('assign_for_peer_review')
            elif version_status == 'Released':
                doc_status = self.get_version_status('assign_for_formal_review')
            else:
                raise TracError("get_activities: unsupported DOC status")
        else:
            doc_status = (self.ticket['description'].split(':')[0]
                          if ':' in self.ticket['description']
                          else 'None')
        activities['01-assigned_for_edition'] = _("Edition of %(status)s", status=doc_status)
        activities['02-assigned_for_peer_review'] = _("Piloting of Peer Review on %(status)s", status=doc_status)
        activities['03-assigned_for_formal_review'] = _("Piloting of Formal Review on %(status)s", status=doc_status)
        activities['04-assigned_for_approval'] = _("Approval of %(status)s", status=doc_status)
        activities['05-approved'] = _("Piloting of Approval of %(status)s", status=doc_status)
        activities['06-assigned_for_release'] = _("Release of %(status)s", status=doc_status)
        activities['closed'] = _("None")

        return activities

    def get_approver(self):
        if self.ticket['status'] == '04-assigned_for_approval':
            approver = self.ticket['owner']
        else:
            approver = None

        return approver

    def get_approvers(self):
        approvers = []
        changes = [change for change in
                   self.component.ticket_module.rendered_changelog_entries(self.req, self.ticket)]
        for change in reversed(changes):
            if ('status' in change['fields'] and
                change['fields']['status']['old'] == '03-assigned_for_formal_review' and
                change['fields']['status']['new'] == '04-assigned_for_approval'):
                break
            elif ('signed as approver by' in change['comment']):
                regexp = r"[\s]+([a-z-]+\.[a-z-]+)[\s(]?"
                match = re.search(regexp, change['comment'])
                if match:
                    approver_name = match.group(1)
                else:
                    continue
                approver_roles = self.get_roles_by_initials(approver_name)
                regexp = '(%s)' % '|'.join(list(approver_roles.keys()))
                match = re.search(regexp, change['comment'])
                if match:
                    approver_role = match.group(1)
                else:
                    approver_role = '(role not set)'
                approvers.append('%s %s' % (approver_role, approver_name))

        return approvers

    def get_author(self):
        """
        Get the author
        """
        if self.ticket['status'] in ('04-assigned_for_approval', '06-assigned_for_release'):
            author = self.get_peer()
        else:
            author = self.ticket['owner']

        return author

    @staticmethod
    def get_authors(ticket):
        """The authors are determined based on the ticket history of changes:
           tracking of ticket owner changes (author owns the ticket in edition mode)

        They include:
          * the ticket owner after selecting the source and pdf files (first time)
          * the ticket owner after changing owner when in the edition mode
          * the ticket owner before leaving the edition mode
          * the ticket owner when returning to the edition mode
        """
        owner = ticket['reporter']
        initial_status = TicketWF.get_WF(ticket).get_initial_status()
        status = initial_status
        authors = OrderedSet()

        users = Users(ticket.env)

        def add_author(owner):
            if (owner not in users.users_by_profile['admin'] or
                owner in chain.from_iterable(list(users.users_by_role.values()))):
                # admin user filtered out if without role
                authors.add(owner)

        # changes are ordered by time,permanent,author
        changelog = ticket.get_changelog()
        changes = deque([{}, {}, {}])

        for time, group in groupby(changelog, lambda x: x[0]):
            changes.popleft()
            changes.append({change[2]: (change[3], change[4], change[1]) for change in group})

            if ('sourcefile' in changes[-1] and changes[-1]['sourcefile'][0] == '' and
                'pdffile' in changes[-1] and changes[-1]['pdffile'][0] == ''):
                # conf manager => author
                if 'owner' in changes[-1]:
                    # what it should be
                    owner = changes[-1]['owner'][1]
                else:
                    # legacy if ticket created and assigned immediately to author
                    owner = changes[-1]['sourcefile'][2]
                add_author(owner)
            elif ('status' not in changes[-1] and
                  status == initial_status and
                  'owner' in changes[-1]):
                # conf manager => author
                # conf manager erroneously keeps ownership - includes legacy
                owner = changes[-1]['owner'][1]
                add_author(owner)
                cond1 = ('sourcefile' in changes[-2] and changes[-2]['sourcefile'][0] == '' and
                         'pdffile' in changes[-2] and changes[-2]['pdffile'][0] == '')
                cond21 = ('sourcefile' in changes[-3] and changes[-3]['sourcefile'][0] == '' and
                          'pdffile' in changes[-3] and changes[-3]['pdffile'][0] == '')
                cond22 = ('sourceurl' in changes[-2])
                if (cond1 or (cond21 and cond22)):
                    authors.discard(changes[-1]['owner'][0])
            elif ('status' in changes[-1] and
                  changes[-1]['status'][0] == initial_status):
                # leave edition
                # legacy if sourcefile and pdffile not changed simultaneously
                status = changes[-1]['status'][1]
                if 'owner' in changes[-1]:
                    owner = changes[-1]['owner'][1]
                    add_author(changes[-1]['owner'][0])
                else:
                    owner = changes[-1]['status'][2]
                    add_author(owner)
            elif ('status' in changes[-1] and
                  changes[-1]['status'][1] == initial_status):
                # reenter edition
                status = changes[-1]['status'][1]
                if 'owner' in changes[-1]:
                    owner = changes[-1]['owner'][1]
                add_author(owner)
            elif ('document' in changes[-1] and 'status' in changes[-2] and
                  changes[-2]['status'][1] == initial_status):
                # tag removal  by admin
                if (owner in users.users_by_profile['admin'] and
                    owner in chain.from_iterable(list(users.users_by_role.values()))):
                    authors.discard(owner)
            else:
                # just keep track of owner and status
                if 'owner' in changes[-1]:
                    owner = changes[-1]['owner'][1]
                if 'status' in changes[-1]:
                    status = changes[-1]['status'][1]

        return authors

    def get_confmgr(self):
        """
        Get the Configuration Manager
        """
        if self.ticket['status'] == '06-assigned_for_release':
            confmgr = self.ticket['owner']
        else:
            confmgr = None

        return confmgr

    def get_certificate_validity_required(self):
        return self.component.certificate_validity_required

    def get_new_status(self):
        """Determines the new status"""
        action = self.action if self.action else 'reassign'
        status = self.get_status(self.ticket)
        new_status = self.component.actions[action]['newstate']

        if new_status == '*':
            triage_field = self.component.actions[action]['triage_field']
            new_status = self.component.actions[action]['triage_status'][self.get_triage_value(triage_field)][status]
            if new_status == '*':
                if action == 'reopen':
                    new_status = self.get_previous_status()
                    # legacy support
                    if new_status in ('04-assigned_for_release', '05-assigned_for_release'):
                        new_status = '06-assigned_for_release'
                else:
                    # 'view' or 'reassign' actions only: status does not change
                    new_status = status

        return new_status

    def get_owner_hint(self, operation, post_id='104'):
        return super(DOCWF, self).get_owner_hint(operation, post_id)

    def get_reassign_labels(self):
        reassign_labels = {}

        reassign_labels['01-assigned_for_edition'] = "reassign for edition"
        reassign_labels['02-assigned_for_peer_review'] = "reassign for piloting of peer review"
        reassign_labels['03-assigned_for_formal_review'] = "reassign for piloting of formal review"
        reassign_labels['04-assigned_for_approval'] = "reassign for approval"
        reassign_labels['05-approved'] = "reassign for piloting of approval"
        reassign_labels['04-assigned_for_release'] = "reassign for release"  # for legacy support
        reassign_labels['05-assigned_for_release'] = "reassign for release"  # for legacy support
        reassign_labels['06-assigned_for_release'] = "reassign for release"

        return reassign_labels

    def get_reviewed_tag(self, action=None):
        action = self.action if action is None else action
        reviewed_tag = self.ticket['document']

        return reviewed_tag

    def get_reviewers_count(self):
        # Number of reviewers that agreed to sign (based on PRF)
        return len(self.get_signers_to_sign('assign_for_approval', '04-assigned_for_approval'))

    def get_signature_comment(self, doc, signatures):
        if self.action == 'assign_for_peer_review':
            version_status = doc.version_status
        else:
            version_status = 'Released'
        return ('%s in status %s '
                'signed as %s' % (self.ticket['pdffile'],
                                  version_status,
                                  'and '.join([_f for _f in self.get_signed_as(signatures) if _f])))

    def get_signature_timestamp(self):
        return self.component.draft_signature_timestamp if self.action == 'assign_for_peer_review' else self.component.released_signature_timestamp

    def get_states_for_role(self, role):

        states = super(DOCWF, self).get_states_for_role(role)
        if role == 'configuration manager':
            # for legacy support
            states.add('04-assigned_for_release')
            states.add('05-assigned_for_release')

        return states

    def get_version_status(self, action=None):
        """Get new tag version status"""
        action = self.action if action is None else action
        tagged_item = self.ticket['summary'].strip('DOC_')
        if action in ('assign_for_peer_review',
                      'abort_peer_review',
                      'assign_for_edition',
                      'return_to_peer_review'):
            status = 'Draft'
        elif action in ('assign_for_formal_review',
                        'abort_formal_review',
                        'return_to_formal_review'):
            status = 'Proposed'
        elif action in ('release',
                        'reopen'):
            status = 'Released'
        if status == 'Released':
            if action == 'reopen':
                try:
                    tg = Tag(self.env, '%s.Released' % tagged_item)
                    version_status = status
                except Exception:
                    version_status = None
            else:
                version_status = status
        else:
            indexes = [tg.status_index
                       for tg in Tag.select(
                           self.env,
                           ["tagged_item='%s'" % tagged_item,
                            "status='%s'" % status],
                           ordering_term='status_index',
                           tag_type='version_tags')]
            if indexes:
                if action in ('abort_peer_review',
                              'abort_formal_review',
                              'return_to_peer_review',
                              'return_to_formal_review'):
                    index = indexes[-1]
                else:
                    index = indexes[-1] + 1
            else:
                if action in ('abort_peer_review',
                              'abort_formal_review',
                              'return_to_peer_review',
                              'return_to_formal_review'):
                    index = None
                else:
                    index = 1
            if index:
                version_status = '%s%s' % (status, index)
            else:
                version_status = None

        return version_status

    def is_action_allowed_core(self, action_allowed, action=None, **kwargs):
        action = self.action if action is None else action

        action_allowed = super(DOCWF, self).is_action_allowed_core(action_allowed, action)

        # No actions are allowed if the DOC ticket is in Edition mode
        if self.ticket['status'] == '01-assigned_for_edition':
            template_cls = cache.Ticket_Cache.get_subclass(self.ticket['type'])
            with template_cls(self.env,
                              self.component.trac_env_name,
                              self.req.authname,
                              self.ticket) as doc:
                repos_status = doc.status(self.ticket['sourcefile'], 'repos-status')
                if repos_status['lock_agent'] == 'trac':
                    action_allowed = (False, "No action is allowed as long as the source file is locked")
                    return action_allowed
                repos_status = doc.status(self.ticket['pdffile'], 'repos-status')
                if repos_status['lock_agent'] == 'trac':
                    action_allowed = (False, "No action is allowed as long as the pdf file is locked")
                    return action_allowed

        # Some actions are dependent upon document status
        version_status = kwargs.get('version_status', None)
        if action in ('assign_for_peer_review',
                      'assign_for_formal_review'):
            if not self.ticket['sourcefile'] or not self.ticket['pdffile']:
                action_allowed = (False, "Source and PDF files have to be selected first")
            else:
                if self.ticket['sourcefile'].endswith('.docm'):
                    # Can restrict according to version status
                    last_rev_repo = util.get_last_path_rev_author(
                        self.env, util.get_url(self.ticket['sourceurl']))[2]
                    if last_rev_repo == util.get_revision(self.ticket['sourceurl']):
                        template_cls = cache.Ticket_Cache.get_subclass(self.ticket['type'])
                        with template_cls(self.env,
                                          self.component.trac_env_name,
                                          self.req.authname,
                                          self.ticket) as doc:
                            version_status = doc.get_version_status(self.ticket['sourcefile'])
                    if (version_status and
                        ((action == 'assign_for_peer_review' and
                          not version_status.startswith('Draft')) or
                         (action == 'assign_for_formal_review' and
                          version_status != 'Released'))):
                        action_allowed = (False, "This action does not match the version status")

        elif action == 'assign_for_approval' and self.ticket['status'] == '03-assigned_for_formal_review':
            childtickets = util.child_tickets_for_tag(self.ticket)
            if not [tkt['signer'] for tkt in childtickets
                    if tkt['signer'] and tkt['resolution'] == 'fixed']:
                action_allowed = (False, "No child PRF for that version status were closed as fixed with no remarks to implement")

        # Check Tag can be removed
        elif action in ('abort_peer_review', 'abort_formal_review', 'reopen'):
            db = self.env.get_db_cnx()
            tag_name = self.ticket['document']
            if tag_name:
                tg = Tag(self.env, tag_name)
                if ((action == 'abort_peer_review' and tg.status == 'Draft') or
                    (action == 'abort_formal_review' and tg.status == 'Proposed') or
                    (action == 'reopen' and tg.status == 'Released')):
                    baselined_tags = list(set([v.baselined_tag for v in BaselineItem.select(self.env, ['name="' + tag_name + '"'], db=db)]))
                    if baselined_tags:
                        action_allowed = (False, "Action is not allowed as the tag to remove - %s - is included in the baseline(s) %s" % (tag_name, baselined_tags))
                    else:
                        cursor = db.cursor()
                        cursor.execute("SELECT ticket FROM ticket_custom WHERE name='document' AND value='%s' AND ticket <> %s" % (tag_name, self.ticket.id))
                        tickets = [int(row[0]) for row in cursor]
                        if tickets:
                            action_allowed = (False, "Action is not allowed as the tag to remove - %s - is used as a baseline in ticket(s) %s" % (tag_name, tickets))
                        else:
                            branches = [branch.id for branch in Branch.select(self.env, ['source_tag="' + tag_name + '"'], db=db)]
                            if branches:
                                action_allowed = (False, "Action is not allowed as the tag to remove - %s - is used as baseline for the branch(es) %s" % (tag_name, branches))

        return action_allowed

    def is_action_filtered(self):
        filtered = False

        if self.action in ('return_to_peer_review',
                           'return_to_formal_review'):
            if (not self.ticket['document'] or
                util.skill_is_unmanaged(self.env, self.ticket['document'])):
                # No tag or unmanaged skill
                filtered = True
            else:
                doc_status = self.ticket['document'].split('.')[-1]
                if ((self.action == 'return_to_peer_review' and
                     not doc_status.startswith('Draft')) or
                    (self.action == 'return_to_formal_review' and
                     not doc_status.startswith('Proposed'))):
                    # Return action not appropriate to existing tag
                    filtered = True

        return filtered

    def set_tag(self):
        # tag as Draft, Proposed, Released
        if (self.action in ('assign_for_peer_review', 'assign_for_formal_review',
                            'release')):
            db = self.env.get_db_cnx()
            panel = VersionTagsAdminPanel
            version_status = self.get_version_status()
            branch_segregation_activated = True if self.env.config.get('artusplugin', 'branch_segregation_activated') == 'True' else False
            tag_data = {}
            tag_data['tag_name'] = "%s.%s" % (self.ticket['summary'].lstrip('DOC_'),
                                              version_status)
            tag_data['ci_name'] = self.ticket['configurationitem']
            tag_data['authname'] = self.req.authname
            tag_data['modification'] = None
            tag_data['amendment'] = None
            version_id = self.ticket['versionsuffix'].lstrip('_').split('.')
            # Detect special case of trunk: standard missing because null
            if branch_segregation_activated and len(version_id) == 2:
                version_id.insert(0, u'0')
            tag_data['standard'] = version_id[0] if branch_segregation_activated else None
            tag_data['edition'] = version_id[1] if branch_segregation_activated else version_id[0]
            tag_data['revision'] = version_id[2] if branch_segregation_activated else version_id[1]
            regexp = r"^(Draft|Proposed|Released)(\d*)$"
            match = re.search(regexp, version_status)
            if match:
                status = match.group(1)
                status_index = match.group(2)
            else:
                status = None
                status_index = None
            tag_data['status'] = status
            tag_data['status_index'] = status_index
            tag_data['component'] = 'False'
            tag_data['source_url'] = self.ticket['sourceurl']
            tag_data['baselined'] = 'False'
            tag_data['buildbot'] = 'False'
            tag_data['version_type'] = None
            tag_data['from_tag'] = None
            tag_data['program_name'] = self.component.program_name
            tag_data['ticket_id'] = self.ticket.id
            panel.create_tag(self.env, self.req.href, panel.cat_type,
                             panel.page_type, tag_data, db)
            tag_data['comment'] = "tag %s" % tag_data['tag_name']
            tag_data['buildbot_progbase'] = "%s/%s" % (
                BuildBotModule.buildbot_projects,
                self.component.trac_env_name)
            tag_data['tag_url'] = panel.tag_url_from_source_url(
                self.env,
                tag_data['tag_name'])
            panel.apply_tag(self.env, self.req.href, panel.cat_type,
                            panel.page_type, tag_data, db)
            self.ticket['document'] = tag_data['tag_name']
            self.ticket['documenturl'] = tag_data['tag_url']
            # Update description field with progress meter for the new status
            if self.action in ('assign_for_peer_review',
                               'assign_for_formal_review'):
                self.ticket['description'] = ('%s:[[TicketQuery(parent=#%s, '
                                              'document=%s, format=progress)]]' % (
                                                  tag_data['tag_name'],
                                                  self.ticket.id,
                                                  tag_data['tag_name']))
            now = datetime.now(utc)
            self.ticket.save_changes('trac',
                                     _('Tag %(tag)s applied', tag=tag_data['tag_name']), now)

    def unset_tag(self):
        # remove tag Draft, Proposed, Released
        if (self.ticket['document']):
            tg = Tag(self.env, self.ticket['document'])
            if ((self.action == 'abort_peer_review' and
                 tg.status == 'Draft') or
                (self.action == 'abort_formal_review' and
                 tg.status == 'Proposed') or
                (self.action == 'reopen' and self.ticket['resolution'] != 'rejected' and
                 tg.status == 'Released')):
                db = self.env.get_db_cnx()
                tag_data = {}
                tag_data['tag_name'] = self.ticket['document']
                tag_data['authname'] = self.req.authname
                VersionTagsAdminPanel.remove_tag(self.env, self.req.href, 'tags_mgmt',
                                                 'version_tags', tag_data, db)
                previous_tag = self.get_previous_tag(tag_data['tag_name'])
                if previous_tag:
                    tg = Tag(self.env, previous_tag)
                    self.ticket['document'] = previous_tag
                    self.ticket['documenturl'] = tg.tag_url
                    status = tg.status
                    if status == 'Released':
                        version_status = status
                    else:
                        version_status = '%s%s' % (status, tg.status_index)
                    self.ticket['description'] = ('%s:[[TicketQuery(parent=#%s, '
                                                  'document=%s, format=progress)]]' % (
                                                      version_status,
                                                      self.ticket.id,
                                                      previous_tag))
                else:
                    self.ticket['document'] = ''
                    self.ticket['documenturl'] = ''
                    self.ticket['description'] = ''
                now = datetime.now(utc)
                self.ticket.save_changes('trac',
                                         _('Tag %(tag)s removed', tag=tag_data['tag_name']), now)

    def update_document(self):
        # Prepare next Draft
        if (self.action == 'assign_for_edition' and
            self.ticket['sourcefile'].endswith('.docm')):

            # version_status is next Draft
            template_cls = cache.Ticket_Cache.get_subclass(self.ticket['type'])
            with template_cls(self.env,
                              self.component.trac_env_name,
                              self.req.authname,
                              self.ticket) as doc:

                doc.update(self.ticket['sourcefile'])
                doc.lock(self.ticket['sourcefile'])

                # Update Source File Header (name/edition/revision/status)
                # (next Draft status)
                doc.update_data(self.ticket['sourcefile'], True)

                # Commit changes
                revision = doc.commit()
                if revision != '':
                    self.ticket['sourceurl'] = '%s?rev=%s' % (
                        util.get_url(self.ticket['sourceurl']),
                        revision)

                    now = datetime.now(utc)
                    self.ticket.save_changes('trac',
                                             '%s status changed to %s by %s' % (
                                                 self.ticket['sourcefile'],
                                                 doc.version_status,
                                                 self.req.authname), now)
                else:
                    # Remove the lock
                    doc.unlock(self.ticket['sourcefile'])

    def with_independence(self):
        with_independence = False
        if self.action in ('assign_for_approval',
                           'reassign_for_approval',
                           'abort_approval',
                           'approve'):
            with_independence = True

        return with_independence


class TicketWorkflowOpTriage(Component):
    """Action to split a workflow based on a field
    """

    implements(ITicketActionController)

    def __init__(self):
        self.config = self.config  # for PyLint
        self.env = self.env  # for PyLint
        self.get_ticket_wf = TicketWF.get_WF
        self.raw_actions = list(self.config.options('ticket-workflow'))
        self.parsed_actions = self.get_parsed_actions(self.raw_actions)
        self.all_status_by_type = self.get_all_status_by_type(self.parsed_actions)
        self.actions = self.get_structured_actions(self.parsed_actions)
        self.restrict_owner = self.env.config.getbool('ticket', 'restrict_owner')
        self.abort_action = self.env.config.get('artusplugin', 'abort_action')
        self.user_profiles = [group.strip() for group in
                              self.env.config.get('artusplugin', 'user_profiles').
                              split(',')]
        self.user_roles = [group.strip() for group in
                           self.env.config.get('artusplugin', 'user_roles').
                           split(',')]
        self.draft_signature_timestamp = self.env.config.get('artusplugin', 'draft_signature_timestamp', 'True')
        self.released_signature_timestamp = self.env.config.get('artusplugin', 'released_signature_timestamp', 'True')
        self.certificate_validity_required = self.env.config.get('artusplugin', 'certificate_validity_required', 'True')
        self.perm = PermissionSystem(self.env)  # pylint: disable=too-many-function-args
        self.ticket_module = TicketModule(self.env)  # pylint: disable=too-many-function-args
        self.all_permissions = self.perm.get_all_permissions()
        self.users = {t[0] for t in self.all_permissions
                      if t[0] not in self.user_profiles and
                      t[0] not in self.user_roles}
        self.dc_url = self.env.config.get('artusplugin', 'dc_url')
        program_data = util.get_program_data(self.env)
        self.trac_env_name = program_data['trac_env_name']
        self.program_name = program_data['program_name']

    # ITicketActionController methods

    def get_ticket_actions(self, req, ticket):
        """Returns a list of (weight, action) tuples that are valid for this
        request and this ticket."""
        # Get the list of actions that can be performed

        # Determine the current status of this ticket.  If this ticket is in
        # the process of being modified, we need to base our information on the
        # pre-modified state so that we don't try to do two (or more!) steps at
        # once and get really confused.
        ticket_wf = TicketWF.get_WF(ticket)(self, req, ticket)
        status = ticket_wf.get_status(ticket)

        actions = [(t[1]['default'], t[0]) for t in list(self.actions.items()) if
                   ticket_wf.get_triage_value(t[1]['triage_field']) in list(t[1]['triage_permissions'].keys()) and
                   (t[1]['oldstates'] == ['*'] or status in t[1]['oldstates'])]
        # Sort list by weight, descending
        actions = sorted(actions, key=lambda x: x[0], reverse=True)

        return actions

    def get_all_status(self):
        """Return a list of all states described by the configuration.
        """
        all_status = set()
        for action_info in list(self.actions.values()):
            all_status.update(action_info['oldstates'])
            all_status.update(action_info['newstate'])
            for status_info in list(action_info['triage_status'].values()):
                all_status.update(list(status_info.values()))
        all_status.discard('*')

        return all_status

    def render_ticket_action_control(self, req, ticket, action):
        """Returns the action control"""

        # Ticket changes are reverted for rendering if submit has failed
        old = new = {}
        if req.chrome and req.chrome['warnings'] and ticket._old:
            old = copy.deepcopy(ticket._old)
            for fieldname in old:
                new[fieldname] = ticket[fieldname]
                ticket[fieldname] = old[fieldname]
            ticket._old.clear()

        ticket_wf = TicketWF.get_WF(ticket)(self, req, ticket, action)
        triage_field = self.actions[action]['triage_field']
        common_label = ''
        control = []  # default to nothing
        hints = util.OrderedSet()

        def control_rollback():
            # Removes label
            del control[-1]
            # Removes 'and'
            if control:
                del control[-1]

        if action != 'view' and not ticket_wf.is_action_filtered():
            new_status = ticket_wf.get_new_status()
            for label, operation in zip(ticket_wf.get_labels(), ticket_wf.get_operations()):
                if operation == 'send_emails':
                    send_resolution = self.env.config.get('ticket-workflow', 'send.triage_set_resolution').split(' -> ')[1].strip()
                    if send_resolution == 'ready to send':
                        continue
                if control and label:
                    control.append(tag(_(' and ')))
                if label:
                    control.append(tag('%s' % label, ' '))
                if new_status != ticket_wf.get_status(ticket):
                    hints.add(_("Next status will be '%(status)s'", status=new_status))
                if operation == 'set_resolution':
                    resolutions_list = self.actions[action]['triage_set_resolution'][ticket_wf.get_triage_value(triage_field)][ticket_wf.get_status(ticket)]
                    resolutions = [x.strip() for x in resolutions_list.split(',')]
                    assert(resolutions)
                    tag_id = action + '_resolve_resolution'
                    if len(resolutions) == 1:
                        control.append(tag(_("as '%(resolution)s' ", resolution=resolutions[0]), tag.input(
                            type='hidden', id=tag_id, name=tag_id,
                            value=resolutions[0])))
                        hints.add(_("The resolution will be set to '%(resolution)s'", resolution=resolutions[0]))
                    else:
                        selected_option = req.args.get(
                            tag_id,
                            self.config.get('ticket', 'default_resolution'))
                        control.append(tag([_('as '), tag.select(
                            [tag.option(x, selected=(x == selected_option or None))
                             for x in resolutions],
                            id=tag_id, name=tag_id)]))
                        hints.add(_("The resolution will be set"))
                elif operation in ('set_owner', 'set_owner_to_peer',
                                   'set_owner_to_self', 'set_owner_to_other',
                                   'set_owner_to_role'):
                    tag_id = action + '_reassign_owner'
                    control.append(tag(_("to"), " "))
                    owner_hint = ticket_wf.get_owner_hint(operation)
                    control.append(owner_hint)
                    owners, selected_owner = ticket_wf.get_owners_and_selected_owner(
                        operation)
                    if req.chrome and req.chrome['warnings'] and old and 'owner' in new:
                        selected_owner = new['owner']
                    if owners is None:
                        owners_tag = tag.input(type='text', id=tag_id,
                                               name=tag_id, value=selected_owner)
                    else:
                        owners_tag = tag.select(
                            [tag.option(x, selected=(x == selected_owner or None))
                             for x in owners],
                            id=tag_id, name=tag_id)
                    control.append(tag(owners_tag))
                    if ((operation == 'set_owner_to_peer' and
                         ticket_wf.with_independence()) or
                        operation == 'set_owner_to_other'):
                        hints.add(_("The owner will change"))
                    elif owners and len(owners) > 1:
                        hints.add(_("The owner may change"))
                elif operation in ('sign_as_author', 'sign_as_approver', 'sign_as_sender'):
                    if (ticket['type'] == 'DOC' and
                        (not ticket['pdffile'] or ticket['pdffile'] == 'N/A' or
                         not ticket['pdfsigned'] or ticket['pdfsigned'] == '0')):
                        control_rollback()
                    else:
                        roles = ticket_wf.get_roles_by_initials(ticket_wf.req.authname)
                        if roles:
                            tag_id = action + '_set_role'
                            control.append(tag([_("as "), tag.select(
                                [tag.option(k, title=roles[k])
                                 for k in list(roles.keys())],
                                id=tag_id, name=tag_id), " "]))
                        hints.add(_("The document will be signed"))
                elif operation == 'tag_document':
                    version_status = ticket_wf.get_version_status()
                    if version_status is not None:
                        astext = ticket['versionsuffix'][1:]
                        if version_status:
                            astext += '.%s' % version_status
                        astext += ' '
                        control.append(tag(_("as "), tag.b(astext)))
                        hints.add(_("A tag will be created"))
                    else:
                        control_rollback()
                elif operation == 'remove_tag':
                    version_status = ticket_wf.get_version_status()
                    if version_status is not None:
                        astext = ticket['versionsuffix'][1:]
                        if version_status:
                            astext += '.%s' % version_status
                        astext += ' '
                        control.append(tag.b(astext))
                        hints.add(_("A tag will be removed"))
                    else:
                        control_rollback()
                elif operation == 'set_version_status':
                    version_status = ticket_wf.get_version_status()
                    if version_status:
                        control.append(tag.b(' %s ' % version_status))
                    else:
                        control_rollback()
                elif operation == 'agree_to_sign':
                    if ticket_wf.signature_agreement():
                        tag_id = action + '_set_role'
                        roles = ticket_wf.get_roles_by_initials(ticket_wf.req.authname)
                        if roles:
                            control.append(tag([_("as "), tag.select(
                                [tag.option(k, title=roles[k])
                                 for k in list(roles.keys())],
                                id=tag_id, name=tag_id), " "]))
                    else:
                        control_rollback()
                elif operation == 'apply_reviewers_signatures':
                    if (not ticket['pdffile'] or ticket['pdffile'] == 'N/A' or
                        not ticket['pdfsigned'] or ticket['pdfsigned'] == '0'):
                        control_rollback()
                    else:
                        hints.add(_("The document will be signed"))
                elif operation == 'send_emails':
                    emails_count = 1  # ECM/FEE
                    if (ticket['type'] == 'ECM' and ticket['ecmtype'] == 'Technical Note') or ticket['type'] == 'FEE':
                        emails_count += len([a for a in Attachment.select(self.env, 'ticket', ticket.id)])
                    else:
                        emails_count += cache.PDFPackage.get_archives_number(ticket)
                    hints.add(_("%s emails will be sent" % emails_count))
                elif operation == 'return_to_review':
                    version_status = ticket_wf.get_version_status()
                    if version_status is not None:
                        astext = ticket['versionsuffix'][1:]
                        if version_status:
                            astext += '.%s' % version_status
                        astext += ' '
                        control.append(tag('on ', tag.b(astext)))
                    else:
                        control_rollback()
                elif operation == '':
                    pass
                else:
                    self.env.log.error("Unsupported operation for 'triage' operation"
                                       " in action '%s'" % action)

            if not control and len(hints) == 0:
                # action will be filtered out
                common_label = ''
            else:
                common_label = self.get_option_for_status(
                    self.actions[action]['name'], action, ticket_wf.get_status(ticket))
                if common_label:
                    if control:
                        common_label = _('%(label)s and ', label=common_label)
                else:
                    if action == 'assign_for_edition':
                        common_label = tag('return to edition for next ',
                                           tag.b('Draft'), ' or ', tag.b('Released'),
                                           ' document status')
                    else:
                        common_label = ' '

        # ticket changes are restored
        if req.chrome and req.chrome['warnings'] and old:
            for fieldname in old:
                ticket[fieldname] = new[fieldname]
                ticket._old[fieldname] = old[fieldname]

        return (common_label, tag(*control), '. '.join(hints))

    def get_ticket_changes(self, req, ticket, action):
        """Returns the change of status, owner and resolution."""
        ticket_wf = TicketWF.get_WF(ticket)(self, req, ticket, action)
        required_permission = ticket_wf.get_required_permission()
        if required_permission not in req.perm(ticket.resource):
            # The user does not have any of the listed permissions, so we won't
            # do anything.
            return {}

        updated = {}
        # Status changes
        updated['status'] = ticket_wf.get_new_status()

        for operation in ticket_wf.get_operations():
            if operation in ('set_owner', 'set_owner_to_peer', 'set_owner_to_self',
                             'set_owner_to_other', 'set_owner_to_role'):
                tag_id = action + '_reassign_owner'
                if tag_id in req.args:
                    # New owner is set through a selector
                    # because several users have already logged in
                    newowner = req.args.get(tag_id)
                else:
                    # There is no selector because there only one user
                    # has ever logged in so the user in fact doesn't change
                    newowner = ticket['owner']
                # If there was already an owner, we get a list, [new, old],
                # but if there wasn't we just get new.
                if type(newowner) == list:
                    newowner = newowner[0]
                updated['owner'] = newowner
            elif operation == 'set_resolution':
                tag_id = action + '_reassign_owner'
                newresolution = req.args.get(action + '_resolve_resolution',
                                             req.args.get(tag_id, self.config.get(
                                                          'ticket',
                                                          'default_resolution')))
                updated['resolution'] = newresolution
            elif operation == 'agree_to_sign':
                if ticket_wf.signature_agreement():
                    role = req.args.get(action + '_set_role',
                                        '(role not set)')
                    updated['signer'] = '%s %s' % (role, req.authname)

        return updated

    def apply_action_side_effects(self, req, ticket, action):
        """ For ECM/FEE and DOC ticket types
        """
        ticket_wf = TicketWF.get_WF(ticket)(self, req, ticket, action)
        ticket_wf.sign_document()
        ticket_wf.set_tag()
        ticket_wf.unset_tag()
        ticket_wf.update_document()
        ticket_wf.send_emails()

    # Helper methods

    def get_option_for_status(self, option, action, status):
        if '|' in option:
            oldstates = self.actions[action]['oldstates']
            my_dict = dict(list(zip(oldstates, [value.strip() for value in option.split('|')])))
            return my_dict.get(status, '')
        else:
            return option

    def get_parsed_actions(self, raw_actions):

        parsed_actions = {}
        for option, value in raw_actions:
            parts = option.split('.')
            action = parts[0]
            if action not in parsed_actions:
                parsed_actions[action] = {'oldstates': '', 'newstate': ''}
            if len(parts) == 1:
                # Base name, of the syntax: old,states,here -> newstate
                try:
                    oldstates, newstate = [x.strip() for x in value.split('->')]
                except ValueError:
                    continue  # Syntax error, a warning will be logged later
                parsed_actions[action]['newstate'] = newstate
                parsed_actions[action]['oldstates'] = oldstates
            else:
                action, attribute = option.split('.')
                parsed_actions[action][attribute] = value

        return parsed_actions

    def get_all_status_by_type(self, parsed_actions):

        # Initial status
        all_status_by_type = {
            'EFR': set([EFRWF.get_initial_status()]),
            'ECR': set([ECRWF.get_initial_status()]),
            'RF': set([RFWF.get_initial_status()]),
            'PRF': set([PRFWF.get_initial_status()]),
            'MOM': set([MOMWF.get_initial_status()]),
            'RISK': set([RISKWF.get_initial_status()]),
            'AI': set([AIWF.get_initial_status()]),
            'MEMO': set([MEMOWF.get_initial_status()]),
            'ECM1': set([ECM1WF.get_initial_status()]),
            'ECM2': set([ECM2WF.get_initial_status()]),
            'FEE': set([FEEWF.get_initial_status()]),
            'DOC': set([DOCWF.get_initial_status()])
        }

        # New status
        for attributes in list(parsed_actions.values()):
            triage_status = dict([(t[0].strip(), t[1].strip()) for t in [item.split('->') for item in (x.strip() for x in attributes['triage_status'].split('//'))]])
            for ticket_type in list(triage_status.keys()):
                status = triage_status[ticket_type]
                if status != '*':
                    all_status_by_type[ticket_type].add(status)

        # For legacy support: state 02-described removed for RF/PRF
        for ticket_type in ('RF', 'PRF'):
            all_status_by_type[ticket_type].add(u'02-described')

        return all_status_by_type

    def get_structured_actions(self, parsed_actions):

        def as_list(prop):
            value = attributes.get(prop, '')
            return [item for item in (x.strip() for x in value.split(',')) if item]

        def as_dict(prop):
            value = attributes.get(prop, '')
            mydict = dict([(t[0].strip(), t[1].strip()) for t in [item.split('->') for item in (x.strip() for x in value.split('//'))]])
            for key in list(mydict.keys()):
                if '|' in mydict[key]:
                    # attributes['oldstates'] are supposed to match one for one values from splitted mydict[key]
                    mydict[key] = dict(list(zip(attributes['oldstates'], [value.strip() for value in mydict[key].split('|')])))
                else:
                    if attributes['oldstates'] == ['*']:
                        oldstates = self.all_status_by_type[key]
                    else:
                        oldstates = [oldstate for oldstate in attributes['oldstates'] if oldstate in self.all_status_by_type[key]]
                    mydict[key] = dict(list(zip(oldstates, [mydict[key].strip()] * len(oldstates))))
            return mydict

        # Fill in the defaults for every action, and normalize them to the desired
        # types
        for action, attributes in list(parsed_actions.items()):
            # Normalize the oldstates
            attributes['oldstates'] = as_list('oldstates')
            # If not specified, an action is not the default.
            attributes['default'] = int(attributes.get('default', 0))
            # Default the 'name' attribute to the name used in the ini file
            if 'name' not in attributes:
                attributes['name'] = action
            # If operations are not specified, that means no operations
            attributes['operations'] = as_list('operations')
            # Normalize triage_labels
            if 'triage_labels' in attributes:
                attributes['triage_labels'] = as_dict('triage_labels')
            # Normalize triage_operations
            if 'triage_operations' in attributes:
                attributes['triage_operations'] = as_dict('triage_operations')
            # Normalize triage_permissions
            if 'triage_permissions' in attributes:
                attributes['triage_permissions'] = as_dict('triage_permissions')
            # Normalize triage_roles
            if 'triage_roles' in attributes:
                attributes['triage_roles'] = as_dict('triage_roles')
            # Normalize triage_status
            if 'triage_status' in attributes:
                attributes['triage_status'] = as_dict('triage_status')
            # Normalize triage_set_resolution
            if 'triage_set_resolution' in attributes:
                attributes['triage_set_resolution'] = as_dict('triage_set_resolution')

        return parsed_actions
