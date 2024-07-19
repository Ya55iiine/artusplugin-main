# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Artus
# All rights reserved.
#
# Author: Michel Guillot <michel.guillot@meggitt.com>

from trac.resource import ResourceNotFound
from artusplugin import _

__all__ = ['Requirement']


def simplify_whitespace(name):
    """Strip spaces and remove duplicate spaces within names"""
    if name:
        return ' '.join(name.split())
    else:
        return name


class Requirement(object):

    def __init__(self, env, primary_key=None, db=None):
        self.env = env
        if not db:
            db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM sqlite_master "
                       "WHERE tbl_name='requirement'")
        row = cursor.fetchone()
        if not row:
            raise ResourceNotFound(_('Table "requirement" does not exist.'))
        if primary_key:
            requirement, ticket = primary_key
            requirement = simplify_whitespace(requirement)
            cursor = db.cursor()
            cursor.execute("SELECT * FROM requirement "
                           "WHERE requirement=%s AND ticket=%s", (requirement, ticket))
            row = cursor.fetchone()
            if not row:
                raise ResourceNotFound(_('The ticket no %(ticket)s has no impact on requirement %(requirement)s.',
                                         ticket=ticket, requirement=requirement))
            self.requirement = self._old_requirement = requirement
            self.ticket = self._old_ticket = ticket
        else:
            self.requirement = self._old_requirement = None
            self.ticket = self._old_ticket = None

    exists = property(fget=lambda self: self._old_requirement is not None and self._old_ticket is not None)

    def delete(self, db=None):
        assert self.exists, 'Cannot delete non-existent Requirement'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info('Deleting Requirement %s' % self.requirement)
        cursor.execute("DELETE FROM requirement WHERE requirement=%s AND ticket=%s", (self.requirement, self.ticket))

        self.requirement = self._old_requirement = None
        self.ticket = self._old_ticket = None

        if handle_ta:
            db.commit()

    def insert(self, db=None):
        assert not self.exists, 'Cannot insert existing Requirement'
        self.requirement = simplify_whitespace(self.requirement)
        self.ticket = simplify_whitespace(self.ticket)
        assert self.requirement, 'Cannot create Requirement with no requirement'
        assert self.ticket, 'Cannot create Requirement with no ticket'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.debug("Creating new Requirement '%s'" % self.requirement)
        cursor.execute("INSERT INTO requirement (requirement,ticket) "
                       "VALUES (%s,%s)",
                       (self.requirement, self.ticket))

        if handle_ta:
            db.commit()

    def update(self, db=None):
        assert self.exists, 'Cannot update non-existent Requirement'
        self.requirement = simplify_whitespace(self.requirement)
        assert self.requirement, 'Cannot update Requirement with no requirement'
        self.ticket = simplify_whitespace(self.ticket)
        assert self.ticket, 'Cannot update Requirement with no ticket no'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info('Updating ticket no "%d" for requirement "%s"' % (self.ticket, self.requirement))
        cursor.execute("UPDATE requirement SET requirement=%s,ticket=%d "
                       "WHERE requirement=%s and ticket=%d",
                       (self.requirement, self.ticket,
                        self._old_requirement, self._old_ticket))

        if handle_ta:
            db.commit()

    def select(cls, env, where_expr_list=None, ordering_term='ticket ASC', db=None):
        if not db:
            db = env.get_db_cnx()
        cursor = db.cursor()
        sql = "SELECT * FROM requirement "
        if where_expr_list:
            sql += "WHERE " + where_expr_list[0] + " "
            del where_expr_list[0]
            for expr in where_expr_list:
                sql += "AND " + expr + " "
        sql += "ORDER BY " + ordering_term
        cursor.execute(sql)
        for requirement, ticket in cursor:
            record = cls(env)
            record.requirement = record._old_requirement = requirement
            record.ticket = record._old_ticket = ticket
            yield record

    select = classmethod(select)
