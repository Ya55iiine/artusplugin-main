# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Artus
# All rights reserved.
#
# Author: Michel Guillot <michel.guillot@meggitt.com>

""" Some useful macros. """

# Genshi
from trac.util.html import html as tag
from trac.util.html import Markup

# Trac
from trac.core import TracError
from trac.ticket import Ticket
from trac.wiki.macros import WikiMacroBase
from trac.wiki.api import parse_args

# Standard lib
import sys

# Same package
from artusplugin.advanced_workflow import TicketWF


class ConfigGetMacro(WikiMacroBase):
    """Display the value of a given config key from a given section.

    Example:

        {{{[[ConfigGet(section=artusplugin,key=clickonce_app_url_MS_Office)]]}}}

    Args:
        section - trac.ini section[[BR]]
        key - section key
    """

    def expand_macro(self, formatter, name, content):
        # Parse arguments from macro invocation
        args, kw = parse_args(content)
        if 'section' in kw and 'key' in kw:
            value = self.config.get(kw['section'], kw['key'])
            if 'exclude' in kw:
                value = value.replace(kw['exclude'], '')
            return tag.div(value)
        else:
            return "'section' and/or 'key' parameters missing"


class RiskStatusMacro(WikiMacroBase):
    """Display a table giving the current risk status for all known skills

    Example:

        {{{[[RiskStatus(section=ticket-custom,key=skill.options)]]}}}

    Args:
        section - trac.ini section[[BR]]
        key - section key holding the skills

    The values are presented in a table structured as follows:

    ||= Skill  =||= Total Rating =||= Avg Rating =||= Risk Count
    ||= Global =||     xx.x       ||     xx.x     ||    xx.x
    ||= Skill1 =||     xx.x       ||     xx.x     ||    xx.x
    ||= Skill2 =||     xx.x       ||     xx.x     ||    xx.x
    ||= ...... =||     xx.x       ||     xx.x     ||    xx.x

    """

    def expand_macro(self, formatter, name, content):
        args, kw = parse_args(content)
        if 'section' in kw and 'key' in kw:
            skills = self.config.get(kw['section'], kw['key'])
            skill = formatter.req.args.get('SKILL', '')

            # Prepending inline CSS definitions
            styles = """ \
<!--
table.RiskStatus { border-collapse:collapse; }
table.RiskStatus th { font-weight: bold; border-style: solid; border-width:1px; border-color: #bbbbbb; background-color: #f7f7f7; padding:10px; }
table.RiskStatus td { border-style: solid; border-width: 1px; border-color: #bbbbbb; }
-->
"""
            # create inline style definition as Genshi fragment
            styles = tag.style(Markup(styles))
            styles(type='text/css')

            # create table
            buff = tag.table(class_='RiskStatus', cellspacing_='0')

            # create table heading
            heading = tag.tr()
            heading(align='center')
            heading(tag.th("Skill"))
            heading(tag.th("Total Rating"))
            heading(tag.th("Avg Rating"))
            heading(tag.th("Risk Count"))
            buff(heading)

            # create table lines

            # Global data
            sql_rqst = """ \
SELECT total((CASE WHEN tc1.value = 'VH' THEN 0.9 WHEN tc1.value = 'H' THEN 0.6 WHEN tc1.value = 'M' THEN 0.4 WHEN tc1.value = 'L' THEN 0.25 WHEN tc1.value = 'VL' THEN 0.1 END)*(CASE WHEN tc2.value = 'VH' THEN 16 WHEN tc2.value = 'H' THEN 8.5 WHEN tc2.value = 'M' THEN 4 WHEN tc2.value = 'L' THEN 2 WHEN tc2.value = 'VL' THEN 0.5 END)) AS 'Total',
round(avg((CASE WHEN tc1.value = 'VH' THEN 0.9 WHEN tc1.value = 'H' THEN 0.6 WHEN tc1.value = 'M' THEN 0.4 WHEN tc1.value = 'L' THEN 0.25 WHEN tc1.value = 'VL' THEN 0.1 END)*(CASE WHEN tc2.value = 'VH' THEN 16 WHEN tc2.value = 'H' THEN 8.5 WHEN tc2.value = 'M' THEN 4 WHEN tc2.value = 'L' THEN 2 WHEN tc2.value = 'VL' THEN 0.5 END)),2) AS 'Avg',
count(id) AS 'Count'
FROM ticket t
LEFT JOIN ticket_custom tc1 ON (t.id = tc1.ticket and tc1.name = 'probability')
LEFT JOIN ticket_custom tc2 ON (t.id = tc2.ticket and tc2.name = 'impact')
WHERE type = 'RISK'
"""
            db = self.env.get_db_cnx()
            cursor = db.cursor()
            cursor.execute(sql_rqst)
            total_rating, avg_rating, risk_count = cursor.fetchone()

            line = tag.tr()
            line(align='center')
            line(tag.th("Global"))
            line(tag.td(total_rating))
            line(tag.td(avg_rating))
            line(tag.td(risk_count))
            buff(line)

            # Skills data
            sql_rqst = """ \
SELECT total((CASE WHEN tc1.value = 'VH' THEN 0.9 WHEN tc1.value = 'H' THEN 0.6 WHEN tc1.value = 'M' THEN 0.4 WHEN tc1.value = 'L' THEN 0.25 WHEN tc1.value = 'VL' THEN 0.1 END)*(CASE WHEN tc2.value = 'VH' THEN 16 WHEN tc2.value = 'H' THEN 8.5 WHEN tc2.value = 'M' THEN 4 WHEN tc2.value = 'L' THEN 2 WHEN tc2.value = 'VL' THEN 0.5 END)) AS 'Total',
round(avg((CASE WHEN tc1.value = 'VH' THEN 0.9 WHEN tc1.value = 'H' THEN 0.6 WHEN tc1.value = 'M' THEN 0.4 WHEN tc1.value = 'L' THEN 0.25 WHEN tc1.value = 'VL' THEN 0.1 END)*(CASE WHEN tc2.value = 'VH' THEN 16 WHEN tc2.value = 'H' THEN 8.5 WHEN tc2.value = 'M' THEN 4 WHEN tc2.value = 'L' THEN 2 WHEN tc2.value = 'VL' THEN 0.5 END)),2) AS 'Avg',
count(id) AS 'Count'
FROM ticket t
LEFT JOIN ticket_custom tc0 ON (t.id = tc0.ticket and tc0.name = 'skill')
LEFT JOIN ticket_custom tc1 ON (t.id = tc1.ticket and tc1.name = 'probability')
LEFT JOIN ticket_custom tc2 ON (t.id = tc2.ticket and tc2.name = 'impact')
WHERE type = 'RISK' AND tc0.value = %s
"""
            db = self.env.get_db_cnx()
            if skill:
                if skill != '%':
                    skills = skill
                for skill in skills.split('|'):
                    cursor = db.cursor()
                    cursor.execute(sql_rqst, (skill,))
                    total_rating, avg_rating, risk_count = cursor.fetchone()

                    line = tag.tr()
                    line(align='center')
                    line(tag.th(skill))
                    line(tag.td(total_rating))
                    line(tag.td(avg_rating))
                    line(tag.td(risk_count))
                    buff(line)

            # Finally prepend prepared CSS styles
            buff = tag(styles, buff)

            return buff
        else:
            return "Macro %s: 'section' and/or 'key' parameters missing" % name


class TicketIndependence(object):

    @staticmethod
    def get_class(ticket):
        if ticket['type'] == 'ECM':
            if 'ecmtype' in ticket.values:
                ticket_type = 'ECM2'
            else:
                TracError("get_macro: unsupported ticket_type: %s" % ticket_type)
        elif ticket['type'] == 'FEE':
            ticket_type = ticket['type']
        elif ticket['type'] == 'DOC':
            ticket_type = ticket['type']
        else:
            ticket_type = ticket['type']
            TracError("get_macro: unsupported ticket_type: %s" % ticket_type)
        cls = getattr(sys.modules['artusplugin.macros'], '%sIndependence' % ticket_type)
        return cls

    def __init__(self, ticket):
        self.ticket = ticket

    def get_independence(self):
        TracError("get_independence: unsupported call on the base class")


class ECM2Independence(TicketIndependence):

    def __init__(self, ticket):
        super(ECM2Independence, self).__init__(ticket)

    def get_independence(self):
        return None


class FEEIndependence(TicketIndependence):

    def __init__(self, ticket):
        super(FEEIndependence, self).__init__(ticket)

    def get_independence(self):
        return None


class DOCIndependence(TicketIndependence):

    def __init__(self, ticket):
        super(DOCIndependence, self).__init__(ticket)

    def get_independence(self):
        return self.ticket['independence'] == '1'


class AuthorsMacro(WikiMacroBase):
    """Display the authors of a ECM/FEE/DOC ticket.

    Example:

        {{{[[Authors(summary=DOC_TF1103_SYS_SES_1.0)]]}}}

    Args:
        summary - the identifier of the ECM/FEE/DOC ticket
    """

    def expand_macro(self, formatter, name, content):
        # Get ticket
        args, kw = parse_args(content)
        if 'summary' not in kw:
            return "'summary' parameter missing"
        tktids = set()
        for tktids in self.env.db_query("""
                SELECT id FROM ticket WHERE summary=%s
                """, (kw['summary'],)):
            break
        if not tktids:
            return "Invalid ticket %s" % kw['summary']

        tktid = tktids[0]
        ticket = Ticket(self.env, tktid)

        # Authors
        authors = TicketWF.get_WF(ticket).get_authors(ticket)
        authors_str = 'Author(s): '
        if authors:
            authors_str += ', '.join(map(str, authors))
        else:
            authors_str += 'None'

        # Independence
        independence_cls = TicketIndependence.get_class(ticket)
        independence = independence_cls(ticket).get_independence()
        if independence is None:
            independence_str = ''
        else:
            independence_str = ' (Verified with independence: %s)'
            if independence is True:
                independence_str %= 'Yes'
            else:
                independence_str %= 'No'

        return tag.div(authors_str + independence_str)
