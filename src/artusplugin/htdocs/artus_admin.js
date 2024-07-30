
function OnAdminDocumentReady() {
	var match = null;
	/* Show filter tip in red */
	if ( $("#filter_value").val() == _("Set the appropriate filter")) {
		$("#filter_value").css('color', 'red').click(function() {
			if ($(this).val() == _("Set the appropriate filter")) {
				$(this).select();
			}
		}).keypress(function() {
			$(this).css('color', 'inherit');
		});
	}
	/*
	 * Manage Version Tags form
	 */
	match = window.location.href.match(/.+\/([^/]+)\/admin\/tags_mgmt\/version_tags\/[^/]+/);
	if (match) {
		ResetScrollPosition();
	    /* "Select all" button behavior */
	    $("#item_select").click(function(event) {
	        $("input[name=sel]").prop('checked', true);
	        event.preventDefault();
	    });
		/* "Deselect all" button behavior */
	    $("#item_deselect").click(function(event) {
	    	$("input[name=sel]").prop('checked', false);
	        event.preventDefault();
	    });
		/* "Discard changes" button behavior */
	    $("input[name=change]:eq(2)").click(function(event) {
	    	/* Discard changes */
	    	discard_baseline_changes();
			/* 'Discard changes' button */
			document.forms['mod_baseline'].change[2].disabled = true;
			/* 'Apply changes' button */
			document.forms['mod_baseline'].change[3].disabled = true;
			/* No more sub-path changes */
	    	unlock_baseline_changes();
	    	/* No need to submit */
	        event.preventDefault();
	    });
		let component = UIComponents.buttons.addReplaceVersionTag;
		component.object = new (component.constructor())();
		component = UIComponents.buttons.applyTag;
		component.object = new (component.constructor())();
    }
	/*
	 * Manage Milestone Tags form
	 */
	match = window.location.href.match(/.+\/([^/]+)\/admin\/tags_mgmt\/milestone_tags\/[^/]+/);
	if (match) {
		ResetScrollPosition();
	    /* "Select all" button behavior */
	    $("#item_select").click(function(event) {
	        $("input[name=sel]").prop('checked', true);
	        event.preventDefault();
	    });
		/* "Deselect all" button behavior */
	    $("#item_deselect").click(function(event) {
	    	$("input[name=sel]").prop('checked', false);
	        event.preventDefault();
	    });
		let component = UIComponents.buttons.addReplaceVersionTag;
		component.object = new (component.constructor())();
		component = UIComponents.buttons.applyTag;
		component.object = new (component.constructor())();
    }
	/*
	 * Manage Documents form
	 */
    if (document.location.pathname.endsWith('/documents')) {
    	/* Show number of selected items in DRL or package */
    	drl_item_show_count();
    	pdf_checkbox_show_count();

	    /* Hide or show buttons / checkboxes used for pdf get function */
	    if ( $("input[id=pdf_packaging]:nth(1)").prop('checked') ) {
	    	pdf_toggle(true);
	    }
	    else {
	        pdf_toggle(false);
	    }

	    /* "Select all" button behavior */
	    $("#drl_select").click(function(event) {
	        $("input[id=drl_item]").prop('checked', true);
	        drl_item_change();
	        drl_item_show_count();
	        event.preventDefault();
	    });
		/* "Deselect all" button behavior */
	    $("#drl_deselect").click(function(event) {
	        $("input[id=drl_item]").prop('checked', false);
	        drl_item_change();
	        drl_item_show_count();
	        event.preventDefault();
	    });
		/* "Discard changes" button behavior */
	    $("input[name=change_drl]:eq(0)").click(function(event) {
	    	/* Discard changes */
	    	discard_drl_changes();
	    	drl_item_show_count();
			/* 'Discard changes' button */
			document.forms['mod_drl'].change_drl[0].disabled = true;
			/* 'Save DRL' button */
			document.forms['mod_drl'].change_drl[1].disabled = true;
			/* 'Save DRL as' button */
			let drl_selector = document.forms['mod_drl'].drl;
			if (drl_selector.options[drl_selector.selectedIndex].value == 'Default DRL') {
				document.forms['mod_drl'].change_drl[2].disabled = false;
			}
			else if (drl_items_checked()) {
				document.forms['mod_drl'].change_drl[2].disabled = false;
			}
			/* 'Select documents for PDF packaging' button */
			document.forms['mod_drl'].pdf_packaging.disabled = false;
	    	/* No need to submit */
	        event.preventDefault();
	    });
	    /* "Select all" button behavior */
	    $("#pdf_select").click(function(event) {
	        $("input[id=pdf_checkbox]").prop('checked', true);
	        pdf_checkbox_change();
	        pdf_checkbox_show_count();
	        event.preventDefault();
	    });
		/* "Deselect all" button behavior */
	    $("#pdf_deselect").click(function(event) {
	    	$("input[id=pdf_checkbox]").prop('checked', false);
	        pdf_checkbox_change();
	        pdf_checkbox_show_count();
	        event.preventDefault();
	    });
		/* "Discard changes" button behavior */
	    $("input[name=change_pdf]").click(function(event) {
	    	/* Discard changes */
	    	discard_pdf_changes();
	    	pdf_checkbox_show_count();
			/* 'Discard changes' button */
			document.forms['mod_drl'].change_pdf.disabled = true;
	    	/* No need to submit */
	        event.preventDefault();
	    });
    }
	/*
	 * Manage Branches form
	 */
	match = window.location.href.match(/.+\/([^/]+)\/admin\/tags_mgmt\/branches\/[^/]+/);
	if (match) {
		let component = UIComponents.buttons.applyBranch;
		component.object = new (component.constructor())();
	}
}

function admin_reqListener(field_id, attribute_name, attribute_value) {
	/* Set the field value */
	var field = $("[id=" + field_id + "]");
	field.prop(attribute_name, attribute_value);
}

function admin_xhr_get(qsParm) {
	var match = window.location.href.match(/\/tracs\/(\w+)\/admin\/tags_mgmt\/([^/?&]+)/);
	var trac_env_name = match[1];
	var panel = match[2];
	var url = this.location.protocol + "//" + this.location.host + "/tracs/" + trac_env_name + "/admin_xhrget?panel=" + panel;
    url += "&key=" + qsParm[0][0] + "&field_name=" + qsParm[0][1] + "&attribute_name=" + qsParm[0][3];
	var oReq = new XMLHttpRequest();
	oReq.onload = function() {
		if (this.status == 200) {
			admin_reqListener(qsParm[0][2], qsParm[0][3], this.responseText);
		}
	};
	oReq.open("get", url);
	oReq.send();
}

function on_mouseover(key, field_name, field_id, field_attribute) {
	var qsParm = new Array();
	qsParm[0] = new Array(3);
	qsParm[0][0] = key;
	qsParm[0][1] = field_name;
	qsParm[0][2] = field_id;
	qsParm[0][3] = field_attribute;
	admin_xhr_get(qsParm);
}

function pdf_toggle(visible) {
    if (visible) {
    	$("#DRLDocSelection #legend").text('PDF files included in the package ');
    	$("#versions").show();
    	$('#drl option:first-child').text('All documents')
    	$(".selected_pdf").show();
    	$("#pdf_get").show();
    	$("input[id=pdf_checkbox]").prop('disabled', false);
    	$("#Options").show();
    	$(".in_drl").hide();
    	$("#drl_select").hide();
    	$("#drl_deselect").hide();
    	$("input[name=change_drl]:eq(0)").hide();
    	$("input[name=change_drl]:eq(1)").hide();
    	$("input[name=change_drl]:eq(2)").hide();
    	$("input[name=drl_as]").hide();
    	$("input[name=change_drl]:eq(3)").hide();
    	$("input[name=drl_item]").prop('disabled', true);
    	$("#pdf_select").show();
    	$("#pdf_deselect").show();
    	$("input[name=change_pdf]").show();
    }
    else {
    	$("#DRLDocSelection #legend").text('Documents included in the DRL ');
    	$("#versions").hide();
    	$('#drl option:first-child').text('Create DRL')
    	$(".selected_pdf").hide();
		$("#pdf_get").hide();
    	$("input[id=pdf_checkbox]").prop('disabled', true);
    	$("#Options").hide();
    	$(".in_drl").show();
    	$("#drl_select").show();
    	$("#drl_deselect").show();
    	$("input[name=change_drl]:eq(0)").show();
    	$("input[name=change_drl]:eq(1)").show();
    	$("input[name=change_drl]:eq(2)").show();
    	$("input[name=drl_as]").show();
    	$("input[name=change_drl]:eq(3)").show();
    	$("input[name=drl_item]").prop('disabled', false);
    	$("#pdf_select").hide();
    	$("#pdf_deselect").hide();
    	$("input[name=change_pdf]").hide();
    }
  }

function remove_from_string(s, t) {
	/*
	 * Remove all occurrences of a token in a string
	 *   s string to be processed
	 *   t token to be removed
	 * returns new string
	 */

	var i = s.indexOf(t);
	var r = "";
	if (i == -1) return s;
	r += s.substring(0,i) + remove_from_string(s.substring(i + t.length), t);
	return r;
}

function discard_baseline_changes() {
	$('form#mod_baseline [name=subpath]').each(function() {
		$(this).val($(this).attr('value'));
	});
}

function discard_drl_changes() {
	$('form#mod_drl #drl_item').each(function() {
		$(this).prop('checked', this.hasAttribute('checked'));
	});
}

function discard_pdf_changes() {
	$('form#mod_drl #pdf_checkbox').each(function() {
		$(this).prop('checked', this.hasAttribute('checked'));
	});
}

function drl_item_show_count() {
	var td_text = '(' + $("#drl_item:checked").length + ' selected)';
	$("#in_drl").text(td_text);
	$(".in_drl").text(td_text);
}

function pdf_checkbox_show_count() {
	var td_text = '(' + $("#pdf_checkbox:checked").length + ' selected)';
	$(".selected_pdf").text(td_text);
}

function drl_items_checked() {
	var drl_items = ( typeof(document.forms['mod_drl'].drl_item.length) != 'undefined' ) ? document.forms['mod_drl'].drl_item : [document.forms['mod_drl'].drl_item];
	for (var i=0; i<drl_items.length; i++) {
		if (drl_items[i].checked == true) return true;
	}
	return false;
}

function subpath_changed() {
	var result = false;
	$('form#mod_baseline [name=subpath]').each(function() {
		if ($(this).val() != $(this).attr('value')) {
			return result = true;
		}
	});
	return result;
}

function drl_item_changed() {
	var result = false;
	$('form#mod_drl #drl_item').each(function() {
		if ($(this).prop('checked') != this.hasAttribute('checked')) {
			return result = true;
		}
	});
	return result;
}

function pdf_checkbox_changed() {
	var result = false;
	$('form#mod_drl #pdf_checkbox').each(function() {
		if ($(this).prop('checked') != this.hasAttribute('checked')) {
			return result = true;
		}
	});
	return result;
}

function lock_baseline_changes() {
	/*
	 * Disables IHM elements that would loose sub-path changes not yet applied
	 */
	/* 'Apply Tag' button */
	document.forms['apply_baselined'].apply.disabled = true;
	/* 'Remove selected items' button */
	document.forms['mod_baseline'].change[1].disabled = true;
	/* 'Add/Replace' button */
	document.forms['add_tag'].add.disabled = true;
	/* 'Update' button */
	document.forms['add_tag'].update.disabled = true;
	/* 'Enter' button */
	document.forms['add_tag'].filter_value.addEventListener("keypress",stopRKey,false)
	/* 'Version Tag to include' selector */
	document.forms['add_tag'].included_tag.disabled = true;
}

function unlock_baseline_changes() {
	/*
	 * Enables IHM elements when no sub-path change may be lost
	 */
	/* 'Apply Tag' button */
	document.forms['apply_baselined'].apply.disabled = false;
	/* 'Remove selected items' button */
	document.forms['mod_baseline'].change[1].disabled = false;
	/* 'Add/Replace' button */
	document.forms['add_tag'].add.disabled = false;
	/* 'Update' button */
	document.forms['add_tag'].update.disabled = false;
	/* 'Enter' button */
	document.forms['add_tag'].filter_value.removeEventListener("keypress",stopRKey,false)
	/* 'Version Tag to include' selector */
	document.forms['add_tag'].included_tag.disabled = false;
}

function subpath_change() {
	/*
	 * Disables or enables IHM elements depending on sub-paths changes being not applied
	 */
	if (subpath_changed()) {
		/* 'Discard changes' button */
		document.forms['mod_baseline'].change[2].disabled = false;
		/* 'Apply changes' button */
		document.forms['mod_baseline'].change[3].disabled = false;
		/* Protect sub-path changes */
		lock_baseline_changes();
	}
	else {
		/* 'Discard changes' button */
		document.forms['mod_baseline'].change[2].disabled = true;
		/* 'Apply changes' button */
		document.forms['mod_baseline'].change[3].disabled = true;
		/* No more sub-path changes */
		unlock_baseline_changes();
	}
}

function drl_item_change() {
	drl_item_show_count();
	/*
	 * Disables or enables IHM elements depending on drl_item changes being not applied
	 */
	var drl_selector;
	if (drl_item_changed()) {
		/* 'Discard changes' button */
		document.forms['mod_drl'].change_drl[0].disabled = false;
		/* 'Save DRL' button */
		drl_selector = document.forms['mod_drl'].drl;
		if (drl_selector.options[drl_selector.selectedIndex].value != 'Default DRL' && drl_items_checked()) {
			document.forms['mod_drl'].change_drl[1].disabled = false;
		}
		/* 'Select documents for PDF packaging' button */
		document.forms['mod_drl'].pdf_packaging.disabled = true;
	}
	else {
		/* 'Discard changes' button */
		document.forms['mod_drl'].change_drl[0].disabled = true;
		/* 'Save DRL' button */
		drl_selector = document.forms['mod_drl'].drl;
		if (drl_selector.options[drl_selector.selectedIndex].value != 'Default DRL') {
			document.forms['mod_drl'].change_drl[1].disabled = true;
		}
		/* 'Select documents for PDF packaging' button */
		document.forms['mod_drl'].pdf_packaging.disabled = false;
	}
	/* 'Save DRL as' button */
	if (drl_items_checked()) {
		document.forms['mod_drl'].change_drl[2].disabled = false;
	}
	else {
		document.forms['mod_drl'].change_drl[2].disabled = true;
	}
}

function pdf_checkbox_change() {
	pdf_checkbox_show_count();
	/*
	 * Disables or enables IHM elements depending on pdf_checkbox changes being not applied
	 */
	if (pdf_checkbox_changed()) {
		/* 'Discard changes' button */
		document.forms['mod_drl'].change_pdf.disabled = false;
	}
	else {
		/* 'Discard changes' button */
		document.forms['mod_drl'].change_pdf.disabled = true;
	}
}

function ResetScrollPosition() {
	var hidx, hidy, form_id;
	if (typeof(document.forms['version_tag_table']) != "undefined") {
		/* First page Version Tags */
		form_id = 'version_tag_table';
	}
	else if (typeof(document.forms['milestone_tag_table']) != "undefined") {
		/* First page Milestone Tags */
		form_id = 'milestone_tag_table';
	}
	else if (typeof(document.forms['mod_baseline']) != "undefined") {
		/* Detail view Version Tags or Milestone Tags */
		form_id = 'mod_baseline';
	}
	else if (typeof(document.forms['apply_not_baselined']) != "undefined") {
		/* Detail view Version Tags */
		form_id = 'apply_not_baselined';
	}
	else if (typeof(document.forms['apply_baselined']) != "undefined") {
		/* Detail view Version Tags */
		form_id = 'apply_baselined';
	}
	else if (typeof(document.forms['mod_drl']) != "undefined") {
		/* First page Documents */
		form_id = 'mod_drl';
	}
	else {
		/* Something wrong somewhere */
		form_id = "undefined";
	}
	if (form_id != "undefined") {
		if (typeof(document.forms[form_id].ScrollX) != "undefined" && typeof(document.forms[form_id].ScrollY) != "undefined") {
			hidx = document.forms[form_id].ScrollX;
			hidy = document.forms[form_id].ScrollY;
			window.scrollTo(hidx.value, hidy.value);
		}
	}
}

function getScroll(axis) {
	  var scrOf = 0;
	  if (axis == 'X') {
		if( typeof( window.pageXOffset ) == 'number' ) {
	      //Netscape compliant
	      scrOf = window.pageXOffset;
	    } else if( document.body && document.body.scrollLeft ) {
	      //DOM compliant
	      scrOf = document.body.scrollLeft;
	    } else if( document.documentElement && document.documentElement.scrollLeft ) {
	      //IE6 standards compliant mode
	      scrOf = document.documentElement.scrollLeft;
	    }
	  }
	  else {
		if( typeof( window.pageYOffset ) == 'number' ) {
		  //Netscape compliant
		  scrOf = window.pageYOffset;
		} else if( document.body && document.body.scrollTop ) {
	      //DOM compliant
		  scrOf = document.body.scrollTop;
		} else if( document.documentElement && document.documentElement.scrollTop ) {
		  //IE6 standards compliant mode
		  scrOf = document.documentElement.scrollTop;
		}
	  }
	  return scrOf;
}

function SaveScrollXY(form_id) {
	if (typeof(form_id) != "undefined" && typeof(document.forms[form_id]) != "undefined" &&
		typeof(document.forms[form_id].ScrollX) != "undefined" && typeof(document.forms[form_id].ScrollY) != "undefined") {
		document.forms[form_id].ScrollX.value = getScroll('X');
		document.forms[form_id].ScrollY.value = getScroll('Y');
	}
}

function SaveSortOrder(form_id, form_type, header, ascending) {
	if (typeof(form_id) != "undefined" && typeof(document.forms[form_id]) != "undefined") {
		if (form_type == 'including') {
			if (typeof(document.forms[form_id]).sort_including != "undefined" &&
				typeof(document.forms[form_id]).asc_including != "undefined") {
				document.forms[form_id].sort_including.value = header;
				document.forms[form_id].asc_including.value = ascending;
			}
		}
		else {
			if (typeof(document.forms[form_id]).sort_included != "undefined" &&
				typeof(document.forms[form_id]).asc_included != "undefined") {
				document.forms[form_id].sort_included.value = header;
				document.forms[form_id].asc_included.value = ascending;
			}
		}
	}
}

function GetSortOrder(form_id, form_type) {
	if (typeof(form_id) != "undefined" && typeof(document.forms[form_id]) != "undefined") {
		if (form_type == 'including') {
			if (typeof(document.forms[form_id]).sort_including != "undefined" &&
				typeof(document.forms[form_id]).asc_including != "undefined") {
				return [document.forms[form_id].sort_including.value,
						document.forms[form_id].asc_including.value];
			}
		}
		else {
			if (typeof(document.forms[form_id]).sort_included != "undefined" &&
				typeof(document.forms[form_id]).asc_included != "undefined") {
				return [document.forms[form_id].sort_included.value,
					document.forms[form_id].asc_included.value];
			}
		}
	}
}

function getReverseOrder(asc) {
	return asc == '1' ? '0' : '1';
}

function setOrder(form_id, form_type, header) {
	if (!subpath_changed()) {
		var ascending;
		var sort_order = GetSortOrder(form_id, form_type);
		if (sort_order[0] == header) {
			// Order: reversed
			ascending = getReverseOrder(sort_order[1]);
			SaveSortOrder(form_id, form_type, header, ascending);
		}
		else {
			// Order: ascending
			ascending = '1';
		}
		SaveScrollXY(form_id);
		if (form_type == 'including') {
			document.location = get_href([['ScrollX', getScroll('X')], ['ScrollY', getScroll('Y')], ['sort_including', header], ['asc_including', ascending]], []);
		}
		else {
			document.location = get_href([['ScrollX', getScroll('X')], ['ScrollY', getScroll('Y')], ['sort_included', header], ['asc_included', ascending]], []);
		}
		set_overlay();
	}
}

function stopRKey(evt) {
	var my_evt = (evt) ? evt : ((event) ? event : null);
	var node = (my_evt.target) ? my_evt.target : ((my_evt.srcElement) ? my_evt.srcElement : null);
	return ((my_evt.keyCode != 13) || (node.type != "text"));
}

function update_version_tag_name() {
	var ci_name_options = document.getElementById('ci_name').options;
	var ci_name = ci_name_options[document.getElementById('ci_name').selectedIndex].text;
	var ci_type = document.getElementsByName('ci_type');
	var version_type = document.getElementsByName('version_type');
	var standard_options, standard, edition, revision;
	var modification, amendment;
	var status_options, status, status_index, tag_name;
	if (ci_type.length) {
		if (ci_type[0].checked == true) {
			// Document - S.E.R.
			edition = document.getElementById('edition').value;
			revision = document.getElementById('revision').value;
			status_options = document.getElementById('status').options;
			status = status_options[document.getElementById('status').selectedIndex].text;
			tag_name = ci_name + '_' + edition + '.' + revision + '.' + status;
			if (status != 'Released') {
				status_index = document.getElementById('status_index').value;
				tag_name += status_index;
			}
			document.getElementById('tag_name').value = tag_name;
		}
		else if (ci_type[1].checked == true) {
			// Component
			if (version_type.length) {
				if (version_type[0].checked == true) {
					// S.E.R.
					if (document.getElementById('standard').hasOwnProperty('options')) {
						standard_options = document.getElementById('standard').options;
						standard = pad(standard_options[document.getElementById('standard').selectedIndex].text, 2);
					}
					else {
						standard = pad(document.getElementById('standard').value, 2);
					}
					edition = pad(document.getElementById('edition').value, 2);
					revision = pad(document.getElementById('revision').value, 2);
					status_options = document.getElementById('status').options;
					status = status_options[document.getElementById('status').selectedIndex].text[0];
					if (document.getElementById('status_index') != null) {
						status_index = pad(document.getElementById('status_index').value, 2);
					}
					else {
						status_index = '';
					}
					document.getElementById('tag_name').value = ci_name + '_' + standard + '.' + edition + '.' + revision + status + status_index;
				}
				else if (version_type[1].checked == true) {
					// M.A.
					modification = document.getElementById('modification').value;
					amendment = document.getElementById('amendment').value;
					status_options = document.getElementById('status').options;
					status = status_options[document.getElementById('status').selectedIndex].text[0];
					if (document.getElementById('status_index') != null) {
						status_index = pad(document.getElementById('status_index').value, 2);
					}
					else {
						status_index = '';
					}
					if (amendment != '') {
						document.getElementById('tag_name').value = ci_name + '_' + modification + '.' + amendment + status + status_index;
					}
					else {
						document.getElementById('tag_name').value = ci_name + '_' + modification + status + status_index;
					}
				}
			}
		}
	}
}

