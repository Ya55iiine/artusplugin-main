# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Artus
# All rights reserved.
#
# Author: Michel Guillot <michel.guillot@meggitt.com>

from trac.resource import ResourceNotFound
from artusplugin import _

__all__ = ['Build']


def simplify_whitespace(name):
    """Strip spaces and remove duplicate spaces within names"""
    if name:
        return ' '.join(name.split())
    else:
        return name


class Build(object):

    def __init__(self, env, primary_key=None, db=None):
        self.env = env
        if not db:
            db = self.env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM sqlite_master "
                       "WHERE tbl_name='build'")
        row = cursor.fetchone()
        if not row:
            raise ResourceNotFound(_('Table "build" does not exist.'))
        if primary_key:
            builder, build_no = primary_key
            builder = simplify_whitespace(builder)
            cursor = db.cursor()
            cursor.execute("SELECT * FROM build "
                           "WHERE builder=%s AND build_no=%s",
                           (builder, build_no))
            row = cursor.fetchone()
            if not row:
                raise ResourceNotFound(_('There is no build no %(build_no)s '
                                         'for builder %(builder)s.',
                                         build_no=build_no, builder=builder))
            self.builder = self._old_builder = builder
            self.build_no = self._old_build_no = build_no
            self.CSCI_tag = row[2]
            self.build_path = row[3]
            self.EOC_tag = row[4]
            self.completed = row[5]
        else:
            self.builder = self._old_builder = None
            self.build_no = self._old_build_no = None
            self.CSCI_tag = None
            self.build_path = None
            self.EOC_tag = None
            self.completed = None

    exists = property(fget=lambda self: self._old_builder is not None and
                      self._old_build_no is not None)

    def update(self, db=None):
        assert self.exists, 'Cannot update non-existent Build'
        self.builder = simplify_whitespace(self.builder)
        assert self.builder, 'Cannot update Build with no builder'
        self.build_no = simplify_whitespace(self.build_no)
        assert self.build_no, 'Cannot update Build with no build no'
        if not db:
            db = self.env.get_db_cnx()
            handle_ta = True
        else:
            handle_ta = False

        cursor = db.cursor()
        self.env.log.info('Updating build "%d" of builder "%s"' % (
            self.build_no, self.builder))
        cursor.execute("UPDATE build SET builder=%s,build_no=%d,CSCI_tag=%s,"
                       "build_path=%s,EOC_tag=%s,completed=%s "
                       "WHERE builder=%s and build_no=%d",
                       (self.builder, self.build_no, self.CSCI_tag,
                        self.build_path, self.EOC_tag, self.completed,
                        self._old_builder, self._old_build_no))

        if handle_ta:
            db.commit()

    def select(cls, env, where_expr_list=None, ordering_term='build_no ASC', db=None):
        if not db:
            db = env.get_db_cnx()
        cursor = db.cursor()
        sql = "SELECT * FROM build "
        if where_expr_list:
            sql += "WHERE " + where_expr_list[0] + " "
            del where_expr_list[0]
            for expr in where_expr_list:
                sql += "AND " + expr + " "
        sql += "ORDER BY " + ordering_term
        cursor.execute(sql)
        for builder, build_no, CSCI_tag, build_path, EOC_tag, completed in cursor:
            record = cls(env)
            record.builder = record._old_builder = builder
            record.build_no = record._old_build_no = build_no
            record.CSCI_tag = CSCI_tag
            record.build_path = build_path
            record.EOC_tag = EOC_tag
            record.completed = completed
            yield record

    select = classmethod(select)
