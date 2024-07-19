# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Artus
# All rights reserved.
#
# Author: Michel Guillot <michel.guillot@meggitt.com>

""" OpenOffice xml handling
    cf http://odfpy.forge.osor.eu/api-for-odfpy.odt (API)
    cf http://mashupguide.net/1.0/html/ch17s04.xhtml (examples) """


from io import StringIO
from odf.dc import Title, Subject, Description
from odf.element import Text
from odf.form import Checkbox
from odf.meta import InitialCreator, CreationDate, Keyword
from odf.style import Style, BackgroundImage, TableCellProperties
from odf.table import TableRow, TableCell, CoveredTableCell
from odf.text import P, H, A

UNCHECKED = None
CHECKED = 'checked'

STRING_VALUE_TYPE = 'string'
DATE_VALUE_TYPE = 'date'


def doc_save(doc, filename):
    """ Saves Ooo document """
    doc.save(filename)


def get_table_rows(table):
    """ Warning: does not return table header rows """
    return [row for row in table.getElementsByType(TableRow) if row.parentNode == table]


def get_row_cells(row):
    """ return all cells of a row in a list """
    return [cell for cell in row.getElementsByType(TableCell) if cell.parentNode == row]


def get_cell_paragraphs(cell):
    """ return all paragraphs of a cell in a list """
    return [paragraph for paragraph in cell.getElementsByType(P) if paragraph.parentNode == cell]


def get_cell_headers(cell):
    """ return all headers of a cell in a list """
    return [header for header in cell.getElementsByType(H) if header.parentNode == cell]


def my_xml(element, level, stream):
    """ Generate PARTIAL XML stream out of the tree structure """
    if element.tagName == "Text":
        element.toXml(level, stream)
    else:
        stream.write(u'<' + element.tagName)
        if element.childNodes:
            stream.write(u'>')
            for elt in element.childNodes:
                my_xml(elt, level + 1, stream)
            stream.write(u'</' + element.tagName + u'>')
        else:
            stream.write(u'/>')


def get_text_from_cells(table, form_cells, form_selected_cells, form_user_cells, form_data):
    """ Get text from a subset of the form cells """
    for row_cell_names, row_object in zip(form_cells, get_table_rows(table)):
        for cell_name, cell_object in zip(row_cell_names, get_row_cells(row_object)):
            if cell_name in form_selected_cells:
                cell_key = form_selected_cells[cell_name]
                if cell_name in form_user_cells:
                    xml = StringIO()
                    my_xml(cell_object, 1, xml)
                    form_data[cell_key] = xml.getvalue()
                else:
                    form_data[cell_key] = ''
                    for paragraph in get_cell_paragraphs(cell_object):
                        for node in paragraph.childNodes:
                            if node.tagName == 'Text':
                                line = node.data
                                break
                        else:
                            line = ''
                        if form_data[cell_key] == '':
                            form_data[cell_key] = line
                        else:
                            form_data[cell_key] += '\n' + line
                    for header in get_cell_headers(cell_object):
                        for node in header.childNodes:
                            if node.tagName == 'Text':
                                line = node.data
                                break
                        else:
                            line = ''
                        if form_data[cell_key] == '':
                            form_data[cell_key] = line
                        else:
                            form_data[cell_key] += '\n' + line
    return form_data


def insert_text_into_cells(table, form_cells, form_selected_cells, data):
    """ Set text in a subset of the form cells """
    for row_cell_names, row_object in zip(form_cells, get_table_rows(table)):
        for cell_name, cell_object in zip(row_cell_names, get_row_cells(row_object)):
            if cell_name in form_selected_cells:
                cell_key = form_selected_cells[cell_name]
                # The paragraph style - common to all paragraphs for cells
                # updated by TRAC - has to be preserved
                # All paragraphs are removed before text is added
                # in order to take into account the new line character
                for paragraph in get_cell_paragraphs(cell_object):
                    style_name = paragraph.getAttribute('stylename')
                    paragraph.parentNode.removeChild(paragraph)
                for header in get_cell_headers(cell_object):
                    style_name = header.getAttribute('stylename')
                    header.parentNode.removeChild(header)
                # A new paragraph is created for each new line character ('\n')
                if data[cell_key]['text']:
                    # it may be null if no value is associated with Document or Milestone
                    for line in data[cell_key]['text'].split('\n'):
                        cell_object.appendChild(P(text=line, stylename=style_name))
                else:
                    cell_object.appendChild(P(text='', stylename=style_name))
                # The following two attributes are required for numeric content
                if data[cell_key]['type'] == DATE_VALUE_TYPE:
                    cell_object.setAttribute("valuetype",
                                             data[cell_key]['type'])
                    cell_object.setAttribute("datevalue",
                                             data[cell_key]['text'])


def add_row_to_table(table, template_row, cell_names, target_cells, data):
    """ Add a row to a table and initialize some of its cells:
        cell_names: the cell names of the template_row
        template_row: the template row used to create the new row
        target_cells: the subset of the created cells to initialize
        data: the data used to initialize the target cells (dictionary or list of dictionaries)
        The first row of the table will not be initialized,
        it is used as a template (style) (template_cells) """
    tr = TableRow(stylename=template_row.getAttribute('stylename'))
    tr.addElement(CoveredTableCell())
    template_cells = get_row_cells(template_row)
    for cell_name, cell_object in zip(cell_names, template_cells):
        # Inherit cell borders from template cells
        tc = TableCell(valuetype="string",
                       stylename=cell_object.getAttribute('stylename'),
                       numbercolumnsspanned=cell_object.getAttribute('numbercolumnsspanned'))
        if cell_name in target_cells:
            cell_key = target_cells[cell_name]
            text_stylename = "Standard"  # default
            for child in cell_object.childNodes:
                if child.tagName == 'text:p':
                    # same as first empty line
                    text_stylename = child.getAttribute('stylename')
                    break
            p = P(stylename=text_stylename)
            if isinstance(data, dict):
                data = [data]
            for elt in data:
                if elt[cell_key]['text']:
                    if elt[cell_key]['link']:
                        p.addElement(A(type="simple", href=elt[cell_key]['link'], text=elt[cell_key]['text']))
                    else:
                        p.addText(elt[cell_key]['text'])
                    p.addText(' ')
            tc.addElement(p)
        tr.addElement(tc)
    table.addElement(tr)


def get_sort_key(style_name, style_family):
    """ Compute a key for sorting styles first by row then by column """
    index = style_name.rfind(".")
    # Sort by tables
    if index == -1:
        table_name = style_name
    else:
        table_name = style_name[:index]
    table_no = table_name.replace('Tableau', '')
    if len(table_no) == 1:
        table_no = '0' + table_no
    # Sort by cells
    if style_family == "table-cell":
        row_no = style_name[index + 2:]
        # following line works only if there are less than 100 rows
        if len(row_no) == 1:
            row_no = '0' + row_no
        col_no = style_name[index + 1]
        key = table_no + row_no + col_no
    elif style_family == "table-row":
        row_no = style_name[index + 1:]
        # following line works only if there are less than 100 rows
        if len(row_no) == 1:
            row_no = '0' + row_no
        key = table_no + row_no
    else:
        key = table_no + '00'
    return key


def remove_background_image(style):
    """ Removes the background image of a style """
    for element in style.getElementsByType(BackgroundImage):
        element.attributes.clear()
        break


def set_background_image(style, href):
    """ Creates if necessary and initializes the background image of a style """
    # existing background ?
    background_image = None
    for element in style.getElementsByType(BackgroundImage):
        background_image = element
        break
    if not background_image:
        # creation
        for properties in style.getElementsByType(TableCellProperties):
            background_image = BackgroundImage()
            properties.addElement(background_image)
            break
    background_image.setAttribute("filtername", "JPEG - Joint Photographic Experts Group")
    background_image.setAttribute("position", "center right")
    background_image.setAttribute("repeat", "no-repeat")
    background_image.setAttribute("actuate", "onLoad")
    background_image.setAttribute("href", href)
    background_image.setAttribute("type", "simple")


def set_cells_protection(table, form_cells, form_selected_cells, ticket_data):
    """ Set or unset protections for a subset of the form cells """
    for row_cell_names, row_object in zip(form_cells, get_table_rows(table)):
        # A row background image is removed in case
        attribute_name = row_object.getAttribute("stylename")
        if attribute_name:
            remove_background_image(table.ownerDocument.getStyleByName(attribute_name))
        for cell_name, cell_object in zip(row_cell_names, get_row_cells(row_object)):
            if cell_name in form_selected_cells:
                cell_key, cell_status, cell_style = form_selected_cells[cell_name]
                cell_object.setAttribute("protected", ticket_data[cell_key]['protection'])
                document = table.ownerDocument
                curstyle = document.getStyleByName(cell_object.getAttribute("stylename"))
                # each cell must have its own style
                if curstyle.getAttribute("name") != cell_style:
                    try:
                        document.getStyleByName(cell_style)
                    except AssertionError:
                        # A new style has to be created
                        newstyle = Style(name=cell_style, family="table-cell")
                        newtablecellproperties = TableCellProperties()
                        newattributes = curstyle.getElementsByType(TableCellProperties)[0].attributes.copy()
                        newtablecellproperties.attributes = newattributes
                        newstyle.appendChild(newtablecellproperties)
                        # The style has to be inserted in the right place
                        my_key = get_sort_key(cell_style, "table-cell")
                        for style in document.automaticstyles.getElementsByType(Style):
                            style_name = style.getAttribute("name")
                            style_family = style.getAttribute("family")
                            key = get_sort_key(style_name, style_family)
                            if my_key < key:
                                break
                        document.automaticstyles.insertBefore(newstyle, style)  # ATTENTION: what i style not defined ?
                        curstyle = newstyle
                    # The new style is associated with the cell
                    cell_object.setAttribute("stylename", cell_style)
                # The cell background image is set or removed
                if ticket_data[cell_key]['background']:
                    set_background_image(curstyle, ticket_data[cell_key]['background'])
                else:
                    remove_background_image(curstyle)


def get_rows_list(table, form_first_row):
    """ Construct the list of cell rows for the body of a table """
    form_rows = []
    form_cols = [first_cell_name[0] for first_cell_name in form_first_row[0]]
    first_row = int(form_first_row[0][0][1])
    last_row = first_row + (len(get_table_rows(table)) - 1)
    for row_index in range(first_row, last_row + 1):
        form_row = []
        for col_name in form_cols:
            cell_name = col_name + str(row_index)
            form_row.append(cell_name)
        form_rows.append(form_row)
    return form_rows


def get_col_cells(table, form_first_cells, mode):
    """ Construct a dictionary of cells for the body of a table """
    form_cells = {}
    for first_cell_name in form_first_cells:
        first_cell_key, status, style_name = form_first_cells[first_cell_name]
        first_row = int(first_cell_name[1:])
        last_row = first_row + (len(get_table_rows(table)) - 1)
        col_name = first_cell_name[0]
        key_prefix = first_cell_key
        key_suffix = 1
        for row_index in range(first_row, last_row + 1):
            cell_name = col_name + str(row_index)
            cell_key = key_prefix + str(key_suffix)
            if mode == 'text':
                form_cells[cell_name] = cell_key
            else:
                form_cells[cell_name] = (cell_key, status, style_name)
            key_suffix += 1
    return form_cells


def get_checkbox_choices(form, form_data):
    """ Get all Ooo form checkbox status """
    for checkbox in form.getElementsByType(Checkbox):
        if checkbox.getAttribute('currentstate') == CHECKED:
            form_data[checkbox.getAttribute('name')] = CHECKED
        else:
            form_data[checkbox.getAttribute('name')] = UNCHECKED


def set_checkbox_choices(form, ticket_data):
    """ Set all Ooo form checkbox status """
    for checkbox in form.getElementsByType(Checkbox):
        if ticket_data[checkbox.getAttribute('name')] == UNCHECKED:
            if checkbox.getAttribute('currentstate') is not None:
                checkbox.removeAttribute('currentstate')
        else:
            checkbox.setAttribute('currentstate', CHECKED)


def get_ticket_choices(form_controls, setkey, ticket_data, form_data):
    """ Get all ticket form checkbox status """
    for key in form_controls:
        if setkey is None:
            ticket_data[key] = form_data[key]
        elif form_controls[key] == setkey:
            ticket_data[key] = CHECKED
        else:
            ticket_data[key] = UNCHECKED


def set_meta_data(meta, meta_data):
    """
    Set document meta data:
        * title
        * subject
        * description
        * initial creator
        * creation date
        * keywords
    Useful for PDF properties
    """
    tagNames = []
    for node in meta.childNodes:
        tagNames.append(node.tagName)
        if node.tagName == 'dc:title':
            if node.childNodes:
                node.childNodes[0].data = meta_data['title']
            else:
                node.appendChild(Text(meta_data['title']))
        elif node.tagName == 'dc:subject':
            if node.childNodes:
                node.childNodes[0].data = meta_data['subject']
            else:
                node.appendChild(Text(meta_data['subject']))
        elif node.tagName == 'dc:description':
            if node.childNodes:
                node.childNodes[0].data = meta_data['description']
            else:
                node.appendChild(Text(meta_data['description']))
        elif node.tagName == 'meta:initial-creator':
            if node.childNodes:
                node.childNodes[0].data = meta_data['initial creator']
            else:
                node.appendChild(Text(meta_data['initial creator']))
        elif node.tagName == 'meta:creation-date':
            if node.childNodes:
                node.childNodes[0].data = meta_data['creation date']
            else:
                node.appendChild(Text(meta_data['creation date']))
        elif node.tagName == 'meta:keyword':
            if node.childNodes:
                node.childNodes[0].data = meta_data['keywords']
            else:
                node.appendChild(Text(meta_data['keywords']))

    if 'dc:title' not in tagNames:
        meta.appendChild(Title(text=meta_data['title']))
    if 'dc:subject' not in tagNames:
        meta.appendChild(Subject(text=meta_data['subject']))
    if 'dc:description' not in tagNames:
        meta.appendChild(Description(text=meta_data['description']))
    if 'meta:initial-creator' not in tagNames:
        meta.appendChild(InitialCreator(text=meta_data['initial creator']))
    if 'meta:creation-date' not in tagNames:
        meta.appendChild(CreationDate(text=meta_data['creation date']))
    if 'meta:keyword' not in tagNames:
        meta.appendChild(Keyword(text=meta_data['keywords']))
