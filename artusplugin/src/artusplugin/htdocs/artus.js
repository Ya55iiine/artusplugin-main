
qa_review_skills = ['DIR', 'PROC-1', 'PROC-2', 'PROC-3', 'PROC-4', 'PROC-5', 'PROC-6', 'PROC-7', 'PROC-8-1', 'PROC-8-2', 'PROC-8-3', 'PROC-8-4',
                    'PROC-9', 'PROC-10', 'PROC-11', 'PROC-12', 'PROC-13'];
qa_audit_skills = ['QMS', 'CLI', 'INT'];

var beforeunload_event_handler_set = false;

$(document).ready(function($) {
	// Run as soon as the DOM hierarchy has been fully constructed
	if (!$("#jqAlert").length)
		// Makes sure JS from alert popup will NOT execute
		// in order to avoid infinite recursive loop
		OnGeneralDocumentReady();
});

$(window).on('load', function() {
	// run when a page is rendered, ie all assets such as images have been completely received
	if (!$("#jqAlert").length)
		// Makes sure we are not in the context of an alert popup
		// otherwise executing JS further would bring about an infinite recursive loop
		OnGeneralPageLoaded();
});

String.prototype.trim = function()
{
	/* Remove leading and ending white spaces */
	/* (including non-breaking spaces not included in \s before IE9) */
	/* cf stackoverflow.com/questions/7935153/regex-match-works-in-ff-chrome-but-not-ie-8 */
	return this.replace(/^[\s\u00A0]+|[\s\u00A0]+$/g,"");
}

String.prototype.startsWith = function(str)
{
	return this.match("^"+str) == str;
}

String.prototype.endsWith = function(str)
{
	return this.match(str+"$") == str;
}

function pad(nb_str, length) {
	/* Pad a number (string) with leading zeroes */
	var str = nb_str;
	while (str.length < length) {
		str = '0' + str;
	}
	return str;
}

function urlEncodeCharacter(c)
{
	return '%' + c.charCodeAt(0).toString(16);
}

function urlDecodeCharacter(str, c)
{
	return String.fromCharCode(parseInt(c, 16));
}

function urlEncode(s)
{
	return encodeURIComponent(s).replace(/\%20/g, '+').replace(/[!'()*~]/g, urlEncodeCharacter);
}

function urlDecode(s)
{
	return decodeURIComponent(s.replace(/\+/g, '%20')).replace(/\%([0-9a-f]{2})/g, urlDecodeCharacter);
}

function set_overlay()
{
	if($("#overlay").length == 0) {
		$("body").prepend('<div id="overlay" class="ui-widget-overlay" style="z-index: 1001;"></div>');
		var target = document.getElementById('overlay');
		var spinner = new Spinner().spin(target);
	}
}

function unset_overlay() {
	$("#overlay").remove();
}

function OnGeneralDocumentReady() {
	/*
	 * Execute some actions after the DOM is ready
	 * (set ticket type, redirect to browser)
	 * see admin.js for equivalent actions
	 * when admin pages are loaded
	 */
	on_logout_click();
	if (typeof g_ticket_type !== "undefined") {
		// filter_ticket_stream
		let component = UIComponents.buttons.CreateTicketSubmitChanges;
		component.object = new (component.constructor())();
		comment_tip();
		if (typeof g_ticket_status !== "undefined") {
			// ticket created
			threaded_comments();
			// Work-flow dynamically updated
			setup_workflow();
			if ((g_ticket_type == 'ECM' && ecm_legacy() == false) || g_ticket_type == 'FEE' || g_ticket_type == 'DOC' ) {
				// If owner of the ticket or Trac admin
				if (g_ticket_status == '01-assigned_for_edition' && $('select#field-sourcefile').length) {
					doc_sourcefile(); /* call this one first - see below */
					doc_lock_unlock(); /* source modified status required for doc_lock_unlock() */
					doc_pdffile();
				}
				doc_tag_admin();
				doc_sourceurl();
				if (g_ticket_type == 'DOC') {
					// Queries to see open ECRs, CCB MOMs
					doc_ecr_mom_link();
				}
				// ECM / FEE / DOC ticket Lock / Unlock descriptions
				var src_field = $("select[name=field_sourcefile]");
				if (src_field.length) {
					var src_file = src_field.val();
					var pdf_file = $("select[name=field_pdffile]").val();
					set_lock_unlock_description(src_file, pdf_file);
				}
				// Go to edition mode if requested
				get_edition_mode();
			}
			if (g_ticket_type == 'MOM' && g_ticket_momform == 'Archived') {
				// Set lock status
				set_mom_lock_status();
			}
		}
	}
	var href = window.location.href;
	var qsParm = parse_query_string();
	var match = href.match(/.+\/[^/]+\/newticket/);
	if (match) {
		// New ticket
		var program_data = GetProgramData();
		var trac_env_name = program_data['trac_env_name'];
		var program_name = program_data['program_name'];
		/* Get type & skill & document if passed through the query string */
		var ticket_type;
		var skill;
		let doc;
		for (let i=0; i < qsParm.length; i++) {
			if (qsParm[i][0] == 'type') {
				ticket_type = qsParm[i][1];
			}
			else if (qsParm[i][0] == 'skill') {
				skill = qsParm[i][1];
			}
			else if (qsParm[i][0] == 'document') {
				doc = qsParm[i][1];
			}
		}
		if (typeof(ticket_type) != "undefined") {
			ticket_type_set(ticket_type);
		}
		else {
	    	var type_options = document.getElementById('field-type').options;
	    	ticket_type = type_options[document.getElementById('field-type').selectedIndex].text;
		}
		if (/^(?:RF|PRF)$/.test(ticket_type)) {
			update_ticket_identifier(ticket_type, program_name);
		}
		else if (/^(?:EFR|ECR)$/.test(ticket_type)) {
			if (typeof(skill) != "undefined") {
				skill_set(skill);
			}
			if (typeof(doc) != "undefined") {
				doc = decodeURIComponent(doc.replace(/\+/g, '%20'));
				$('#field-document').val(doc);
			}
			if (ticket_type == 'ECR') on_ecrtype_change();
			on_skill_change(ticket_type, program_name, '/tracs/' + trac_env_name + '/browser/tags?caller=tECR');
		}
		else if (/^(?:MOM|RISK|AI|MEMO)$/.test(ticket_type)) {
			if (typeof(skill) != "undefined") {
				skill_set(skill);
			}
			if (ticket_type == 'MOM') {
				on_skill_change(ticket_type, program_name, '/tracs/' + trac_env_name + '/browser/tags');
				qmsref_show_hide();
			}
			else if (ticket_type == 'RISK') {
				on_skill_change(ticket_type, program_name, '/tracs/' + trac_env_name + '/browser/tags');
			}
			else if (ticket_type == 'AI') {
				on_skill_change(ticket_type, program_name, '/tracs/' + trac_env_name + '/browser/tags');
				aitype_show_hide();
			}
			else {
				update_ticket_identifier(ticket_type, program_name);
			}
		}
		else if (ticket_type == 'DOC') {
			on_skill_change(ticket_type, program_name, '/tracs/' + trac_env_name + '/browser/tags');
		}
		else if (ticket_type == 'ECM' && ecm_legacy() == false) {
			on_ecmtype_change(ticket_type, program_name);
		}
		else if (ticket_type == 'FEE') {
			fromfee_filter();
		}
	    browse_for_tag();
	}
	else {
		match = href.match(/admin\/tags_mgmt/);
		if (match) {
			OnAdminDocumentReady();
		}
		else {
			match = href.match(/attachment\/ticket/);
			if (match) {
				attachment_from();
				for (var i=0; i < qsParm.length; i++) {
					if (qsParm[i][0] == 'action' && qsParm[i][1] == 'new') {
						let component = UIComponents.buttons.addAttachment;
						component.object = new (component.constructor())();
					}
					else if (qsParm[i][0] == 'action' && qsParm[i][1] == 'delete') {
						let component = UIComponents.buttons.deleteAttachment;
						component.object = new (component.constructor())();
					}
				}
			}
			else {
				match = href.match(/.+\/[^/]+\/ticket\/(\d+)/);
				if (match) {
					for (let i=0; i < qsParm.length; i++) {
						if (qsParm[i][0] == 'action' && qsParm[i][1] == 'delete') {
							return;
						}
					}
					var ticket_id = $("h1.summary").text();
					if (ticket_id.startsWith("ECR_")) {
						/* Get document if passed through the query string */
						let doc;
						for (let i=0; i < qsParm.length; i++) {
							if (qsParm[i][0] == 'document') {
								doc = qsParm[i][1];
							}
						}
						if (typeof(doc) != "undefined") {
							doc = decodeURIComponent(doc.replace(/\+/g, '%20'));
							$('#field-document').val(doc);
						}
						on_ecrtype_change();
					}
					else if (ticket_id.startsWith("EFR_")) {
						/* Get document if passed through the query string */
						let doc;
						for (let i=0; i < qsParm.length; i++) {
							if (qsParm[i][0] == 'document') {
								doc = qsParm[i][1];
							}
						}
						if (typeof(doc) != "undefined") {
							doc = decodeURIComponent(doc.replace(/\+/g, '%20'));
							$('#field-document').val(doc);
						}
					}
					else if (ticket_id.startsWith("RISK_")) {
						/* Modify Ticket rating field */
						on_evaluation_change();
					}
					else if (ticket_id.startsWith("ECM_") && ecm_legacy() == false) {
						if (ecmtype_get() == 'Document Delivery') {
							$("#child_tickets").hide();
						}
						else {
							if (g_ticket_status == "05-assigned_for_sending") {
								distribution_filter();
							}
						}
					}
				}
			}
		}
	}
	$('.tooltip').tooltipster({
		theme: 'tooltipster-noir',
		contentCloning: true,
		multiple: true,
		interactive: true
	});
}

function OnGeneralPageLoaded() {
	/*
	 * Execute some actions after the page is completely loaded
	 */
	var qsParm = parse_query_string();
	for (var i=0; i < qsParm.length; i++) {
		if (qsParm[i][0] == 'sourceurl') {
			var sourceurl = qsParm[i][1];
			if (sourceurl != "") {
				/* Trigger auto preview */
				$('input#field-blocking').trigger(jQuery.Event('keypress', { keycode: 13 }));
			}
			break;
		}
	}
}

function GetTracEnvName() {
	var href = window.location.href;
	var match = href.match(/.+\/tracs\/([^/]+)(?:\/)?/);
	if (match && match.length == 2) {
		return match[1];
	}
	else {
		return null;
	}
}

function GetProgramData() {
	var href = window.location.href;
	var match = href.match(/.+\/tracs\/([^/]+)(?:\/)?/);
	if (match && match.length == 2) {
		var data = [];
		data['trac_env_name'] = match[1];
		if ((data['trac_env_name'] != "SB" && data['trac_env_name'].endsWith("SB")) || data['trac_env_name'].endsWith("FF")) {
			data['program_name'] = data['trac_env_name'].substr(0,data['trac_env_name'].length-2);
		}
		else {
			data['program_name'] = data['trac_env_name'];
		}
		return data;
	}
	else {
		return null;
	}
}

function GetProgramUrl() {
	/* Program url */
	var href = window.location.href;
	var match = href.match(/.+(\/tracs\/[^/]+)(?:\/)?/);
	if (match && match.length == 2) {
		return match[1];
	}
	else {
		return null;
	}
}

function insertAfter(newNode, referenceNode) {
	referenceNode.parentNode.insertBefore(newNode, referenceNode.nextSibling);
}

function on_logout_click() {
	/* Displays a pop-up when Logout link is clicked */
	$('[name=logout]').click(function(){
		let alert_msg = _("As your authentication credentials are stored in the browser memory, " +
		"the only way to logout is to close all instances of your browser");
		jqAlert(alert_msg, null, null);
		return false;
	});
}

function comment_tip() {
	var comment_tip_1 = '#comment_help_1';
	if (typeof g_ticket_status !== "undefined" &&
			g_ticket_status.match("^(?:07-assigned_for_closure_actions|closed)$")) {
        if (g_ticket_type.match("^(?:ECR|RF|PRF)$")) {
        	comment_tip_1 = '#comment_help_2';
        	if (g_ticket_type.match("^(?:RF|PRF)$")) {
        		var comment_tip_2 = '#comment_help_3';
        	}
        }
        else if (g_ticket_type == 'EFR') {
        	comment_tip_1 = '#comment_help_4';
        }
	}
	$('h3#edit').attr("style", "display:inline").after("<img id='comment-help' src='/htdocs/help-mini.jpg' />");
	$('img#comment-help').attr({
		style: "margin: 0em 0.5em;vertical-align:middle;",
		"data-tooltip-content": comment_tip_1,
		"class": 'tooltip'
	});
	if (typeof comment_tip_2 !== "undefined") {
		$('img#comment-help').tooltipster({
			theme: 'tooltipster-noir',
			content: $(comment_tip_2),
			multiple: true,
			side: 'bottom',
			delay: 900
		});
	}
}

function doc_sourceurl() {
	if (typeof g_doc_urltracbrowse != 'undefined' && typeof g_doc_sourceurl != 'undefined') {
		$('input#field-sourceurl').after('<input value="Browse" name="sourceurl_browse" type="button" style="margin-left:10px;" onclick="location.href=\'' + g_doc_urltracbrowse + '\'" title="' + g_doc_sourceurl + '" />');
	}
	/* Get sourceurl from the query string */
	var sourceurl_updated = false;
	var qsParm = parse_query_string();
	for (var i=0; i < qsParm.length; i++) {
		if (qsParm[i][0] == 'sourceurl') {
			var sourceurl = qsParm[i][1];
			if (sourceurl != "") {
				var unescaped_sourceurl = unescape(sourceurl);
				$("#field-sourceurl").val(unescaped_sourceurl);
				sourceurl_updated = true;
			}
			break;
		}
	}
	if (sourceurl_updated == false){
		// Source url update
		var data = {};
		data.ticket_id = ticketid_get();
		var async = true;
		artus_xhr("sourceurl", data, async, "GET");
	}
}

function get_edition_mode() {
	// Automatic locking required ?
	var data = {};
	data.ticket_id = ticketid_get();
	var async = true;
	artus_xhr("sourcefile", data, async, "GET");
}

function set_edition_mode() {
	// Automatic locking required
	$('input#lock').click();
}

function ticket_header_doc_locker() {
	if (g_ticket_status != 'closed') {
		get_src_locker();
		get_pdf_locker();
	}
}

function doc_lock_unlock() {
	// Set lock/unlock status
	if (typeof g_doc_lock != 'undefined' && g_doc_lock == true) {
		$("input#lock").prop('checked', true);
		$("input#unlock").prop('checked', false);
	}
	else {
		$("input#lock").prop('checked', false);
		$("input#unlock").prop('checked', true);
	}
	// Change action on lock
	$('input#lock').change(check_external_lock_on_src);
	// Change action on unlock
	$('input#unlock').change(unlock_change);
	// Set focus
	if (typeof g_doc_lock != 'undefined' && g_doc_lock == true) {
		unset_focus('lock');
		if (!$("#src_wc_status").length) {
			/* Source not yet modified */
			unset_focus('unlock');
			set_focus('edit');
		}
		else {
			set_focus('unlock');
			unset_focus('edit');
		}
	}
	else {
		set_focus('lock');
		unset_focus('unlock');
		unset_focus('edit');
	}
	if (typeof g_doc_lock != 'undefined' && g_doc_lock == true) {
		// Disable source and pdf file selectors
		$('select#field-sourcefile').prop('disabled', true);
		$('select#field-pdffile').prop('disabled', true);
	}
	else {
		// Enable source and pdf file selectors
		$('select#field-sourcefile').prop('disabled', false);
		$('select#field-pdffile').prop('disabled', false);
	}

	// Take care of View eyes
	on_sourcefile_change();
	on_pdffile_change();

	// Background color
	if (typeof g_doc_lock != 'undefined' && g_doc_lock == true) {
		$('fieldset[name=working-copy]').css({'background-color': '#F4FFF4',
											  'box-shadow': 'none'});
	}
	else {
		$('fieldset[name=working-copy]').css({'background-color': '',
											  'box-shadow': ''});
	}
	// Set activation
	if (typeof g_doc_lock_unlock_disabled != 'undefined') {
		unset_focus('lock');
		unset_focus('unlock');
		$('input[name=lock_unlock]').prop('disabled', true);
	}
}

function doc_sourcefile() {
	$('select#field-sourcefile').attr({
		style: "margin: 0em 0.5em;vertical-align:middle;",
		"data-tooltip-content": '#sourcefile_tooltip',
		"class": 'tooltip'
	});
	if (typeof g_doc_sourcefile_button_label != 'undefined') {
		$('select#field-sourcefile').after(
				'<span style="border:0px solid red;padding:6px 3px 9px 3px;margin-left:10px"><input value="' +
				g_doc_sourcefile_button_label + '" name="source_edit" id="source_edit" type="button" title="' +
				g_doc_sourcefile_button_label + ' the source file (working copy on the Trac server)" /></span>');
	}
	$('input#source_edit').click(edit_view_file);
	if (typeof g_doc_lock != 'undefined' && g_doc_lock == true) {
		// Show source file modification
		var data = {};
		data.ticket_id = ticketid_get();
		var async = true;
		artus_xhr("src_wc_status", data, async, "GET");
	}
	// Setup View eye
	if (!$('a#sourcefile').length) {
		$('select#field-sourcefile').after(
		'<a id="sourcefile" href="" title=""><img src="/htdocs/eye.png"></img></a>')
	}
}

function doc_pdffile() {
	$('select#field-pdffile').attr({
		style: "margin: 0em 0.5em;vertical-align:middle;",
		"data-tooltip-content": '#pdffile_tooltip',
		"class": 'tooltip'
	});
	$('select#field-pdffile').after(
			'<input value="View" name="pdf_view" id="pdf_view" type="button" style="margin-left:10px;" title="View the PDF file (working copy on the Trac server)" />');
	if (typeof g_doc_pdffile_button_href != 'undefined') {
		$('input#pdf_view').attr('onclick', 'window.open(\'' + g_doc_pdffile_button_href + '\', \'_self\')');
	}
	if (typeof g_doc_lock != 'undefined' && g_doc_lock == true) {
		// Show PDF file modification
		var data = {};
		data.ticket_id = ticketid_get();
		var async = true;
		artus_xhr("pdf_wc_status", data, async, "GET");
	}
	// Setup View eye
	if (!$('a#pdffile').length) {
		$('select#field-pdffile').after(
		'<a id="pdffile" href="" title=""><img src="/htdocs/eye.png"></img></a>')
	}
}

function doc_tag_admin() {
	if (typeof g_doc_tag_admin != 'undefined') {
		$('#field-document').removeAttr('readonly').attr('style', 'background-color:white');
	}
}

function doc_ecr_mom_link() {
	var data = {};
	data.skill = skill_get();
	data.ticket_id = ticketid_get();
	data.milestone = milestone_get();
	var async = true;
	artus_xhr("ecr_mom_report_url", data, async, "GET");
}

function attachment_from() {
	/* selector[0] => From filesystem / selector[1] => From repository */
	var selector = document.getElementsByName('attachment_source');
	if (selector.length) {
		if (selector[0].checked == true) {
			document.getElementById('filesystem').style.display = "";
		}
		else {
			document.getElementById('filesystem').style.display = "none";
		}
		if (selector[1].checked == true) {
			document.getElementById('repository').style.display = "";
			document.getElementById('rename_checklist').style.display = "";
		}
		else {
			document.getElementById('repository').style.display = "none";
			document.getElementById('rename_checklist').style.display = "none";
		}
	}
}

function attachment_from_repository() {
	/*
	 * Redirect to attachment from repository
	 * selection form
	 */
	var href = window.location.href;
	var match = href.match(/\/([^/]+)\/(?:raw-)?attachment\/([^/]+)(?:\/([^?/]+)[?/]{1,2}(?:.*))?$/);
	if (match) {
		if (match.length == 4) {
			var trac_env_name = match[1];
			var realm = match[2];
			if (realm == 'ticket') {
				var attachment_tid = match[3];
				href = '/tracs/' + trac_env_name + '/browser?attachment_tid=' + attachment_tid;
			}
		}
	}
	return href;
}

function ticketid_get() {
	var match = window.location.href.match(/\/tracs\/\w+\/ticket\/(\d+)/)
	if (match !== null) {
		return match[1];
	}
	else {
		return '';
	}
}

function ticket_type_set(val) {
	/*
	 * Set ticket type selector value
	 */
	var type_options = document.getElementById('field-type').options;
	for (var i=0; i < type_options.length; i++) {
		if (type_options[i].text == val) {
			document.getElementById('field-type').selectedIndex = i;
			break;
		}
	}
}

function ecmtype_get() {
	if (typeof g_ticket_ecmtype !== "undefined") {
		// Ticket created
		return g_ticket_ecmtype;
	}
	else {
		// Ticket in creation
		return $( "select#field-ecmtype option:selected" ).text();
	}
}

function momtype_get() {
	if (typeof g_ticket_momtype !== "undefined") {
		// Ticket created
		return g_ticket_momtype;
	}
	else {
		// Ticket in creation
		return $( "select#field-momtype option:selected" ).text();
	}
}

function momtype_set(optionvalue) {
	/*
	 * Set MOM type selector value
	 */
	var options = document.getElementById('field-momtype').options;
	for (var i=0; i < options.length; i++) {
		if (options[i].text == optionvalue) {
			document.getElementById('field-momtype').selectedIndex = i;
			break;
		}
	}
}

function skill_get() {
	if (typeof g_ticket_skill !== "undefined") {
		// Ticket created
		return g_ticket_skill;
	}
	else {
		// Ticket in creation
		return $( "select#field-skill option:selected" ).text();
	}
}

function skill_set(val) {
	$("select#field-skill").val(val);
}

function configurationitem_get() {
	return $("#field-configurationitem").val();
}

function configurationitem_set(val) {
	$("#field-configurationitem").val(val);
}

function branch_get() {
	/* Get branch from parent group of selected configuration item */
	return $("#field-configurationitem option:selected").closest('optgroup').attr('label');
}

function keywords_get() {
	return $("#field-keywords").val();
}

function keywords_set(val) {
	$("#field-keywords").val(val);
}

function fromversion_get() {
	return $("#field-fromversion").val();
}

function fromecm_get() {
	return $("#field-fromecm").val();
}

function fromfee_get() {
	return $("#field-fromfee").val();
}

function evolref_get() {
	return $("#field-evolref").val();
}

function customer_get() {
	return $("#field-customer").val();
}

function program_get() {
	return $("#field-program").val();
}

function application_get() {
	return $("#field-application").val();
}

function changetype_get() {
	return $("#field-changetype").val();
}

function changetype_set(val) {
	$("#field-changetype").val(val);
}

function versionsuffix_get() {
	return $("#field-versionsuffix").val();
}

function versionsuffix_set(val) {
	$("#field-versionsuffix").val(val);
}

function sourcetype_get() {
    return $("#field-sourcetype").val();
}

function sourcetype_set(val) {
    if (val != "") {
		$("#field-sourcetype option[value='" + val + "']").prop('selected', true);
    }
}

function pdfsigned_get() {
    // returns true or false
    return $("#field-pdfsigned").prop("checked");
}

function pdfsigned_set(val) {
	$("#field-pdfsigned").prop("checked", val);
	$("#field-pdfsigned").val(val ? "1" : "0");
	$("#field-checkbox-pdfsigned").val(val ? "1" : "0");
}

function independence_get() {
	// returns true or false
	return $("#field-independence").prop("checked");
}

function independence_set(val) {
	$("#field-independence").prop("checked", val);
	$("#field-independence").val(val ? "1" : "0");
	$("#field-checkbox-independence").val(val ? "1" : "0");
}

function controlcategory_get() {
	return $("#field-controlcategory").val();
}

function controlcategory_set(val) {
	if (val != "") {
		$("#field-controlcategory").val(val);
	}
}

function submittedfor_get() {
	return $("#field-submittedfor").val();
}

function submittedfor_set(val) {
	if (val != "") {
		$("#field-submittedfor").val(val);
	}
}

function milestone_get() {
	return $("#field-milestone").val();
}

function fromname_get() {
	return $("#field-fromname").val();
}

function toname_get() {
	return $("#field-toname").val();
}

function toname_set(val) {
	$("#field-toname").val(val);
}

function fromemail_get() {
	return $("#field-fromemail").val();
}

function toemail_get() {
	return $("#field-toemail").val();
}

function toemail_set(val) {
	$("#field-toemail").val(val);
}

function fromphone_get() {
	return $("#field-fromphone").val();
}

function tophone_get() {
	return $("#field-tophone").val();
}

function tophone_set(val) {
	$("#field-tophone").val(val);
}

function carboncopy_get() {
	return $("#field-carboncopy").val();
}

function ecm_legacy() {
	return $("#h_milestone").length == 0;
}

function itemsdisplay_set(data) {
	$("#itemsdisplay").replaceWith("<div id='itemsdisplay'>" + data + "</div>");
	$("#itemsdisplay").css({"margin": "0 auto", "padding": "1rem 0", "border-spacing": "0"});
}

function browse_for_tag() {
	/*
	 * If document not yet selected redirect to the trac browser in order to select the tag
	 */
	if(g_ticket_type == 'RF' || g_ticket_type == 'PRF') {
		if ($('#field-document').val() == '') {
			let trac_env_name = window.location.href.match(/\/tracs\/(\w+)\/newticket/)[1];
			let outputMsg = "You are redirected to the Source Browser in order to select the document on which you want to create a " +
				g_ticket_type + ". When the document FOLDER is selected, a \"Create " + g_ticket_type + "...\" button appears. Just click on it !"
			jqAlert(outputMsg, null, function() {
				set_overlay();
				document.location = '/tracs/' + trac_env_name + '/browser/tags/versions?caller=t' + g_ticket_type;
			});
		}
	}
}

function parse_query_string(query_string) {
	/*
	 * Return an allocated two-dimensional array
	 * with the (key,value) pairs extracted from the query string
	 */
	var qsParm = new Array();
	if (query_string === undefined) {
		if (document.location.search != "") {
			query_string = document.location.search.substring(1);
		}
		else {
			query_string = ""
		}
	}

	if (query_string  != "") {
	    var parms = query_string.split('&');
	    for (var i=0; i<parms.length; i++) {
	        var pos = parms[i].indexOf('=');
	        var key = parms[i].substring(0,pos);
	        var val = parms[i].substring(pos+1);
	        qsParm[i] = new Array(2);
	        qsParm[i][0] = key;
	        qsParm[i][1] = val;
	    }
	}
	return qsParm;
}

function typeOf(value) {
	/* typeof [] produces 'object' instead of 'array'.
	 * That isn't totally wrong since arrays in JavaScript inherit from objects, but it isn't very useful.
	 * The typeOf function will recognize arrays. Cf http://javascript.crockford.com/remedial.html
	 */
    var s = typeof value;
    if (s === 'object') {
        if (value) {
            if (typeof value.length === 'number' &&
                    !(value.propertyIsEnumerable('length')) &&
                    typeof value.splice === 'function') {
                s = 'array';
            }
        } else {
            s = 'null';
        }
    }
    return s;
}

function date_is_valid(my_date) {
	try {
		$.datepicker.parseDate('yy-mm-dd',my_date);
		return true;
	}
	catch(e) {
		return false;
	}
}

function new_ticket_form(trac_env_name) {
    var ticket_type = document.getElementById('field-type').options[document.getElementById('field-type').selectedIndex].text;
    window.location.href = '/tracs/' + trac_env_name + '/newticket?type=' + ticket_type;
    set_overlay();
}

function update_ticket_identifier(ticket_type, program_name) {
	var identifier;
	if (ticket_type == "RF" || ticket_type == 'PRF') {
        var document_name = $("#field-document").val();
        if (document_name != '') {
        	identifier = ticket_type + '_' + document_name;
        	var assignee = $("#field-owner option:selected").text();
        	identifier += '_' + assignee;
        }
        else {
        	identifier = '';
        }
	}
	else if (/^(?:EFR|ECR|MOM|RISK|AI|MEMO)$/.test(ticket_type)) {
		if (ticket_type == "EFR" && program_name != "TQ") {
			identifier = ticket_type + '_' + program_name + '_xxx';
		}
		else {
			var skill = skill_get();
			if (skill == "") {
				jqAlert("No skill is defined !");
				return false;
			}
			identifier = ticket_type + '_' + program_name + '_' + skill;
			if (/^(?:EFR|ECR|MEMO)$/.test(ticket_type)) {
				identifier += '_xxx';
			}
			else if (ticket_type == 'MOM') {
				var duedate = $("#field-duedate").val();
				if (duedate == "" || !date_is_valid(duedate)) {
					duedate = 'YYYY-MM-DD';
				}
				var milestonetag_short = 'MMM.MilestoneStatus';
				var milestonetag = $("#field-milestonetag option:selected").text();
				if (milestonetag != "") {
					milestonetag_short = milestonetag.slice(milestonetag.lastIndexOf('_')+1);
				}
				var milestone_short = 'MMM';
				var milestone = $("#field-milestone option:selected").text();
				if (milestone != "") {
					milestone_short = milestone.slice(milestone.lastIndexOf('_')+1);
				}
				var momtype = $("#field-momtype option:selected").text();
				if (momtype == 'CCB') {
					/* Change Control Board */
					identifier += '_CCB_' + milestonetag_short;
				}
				else if (momtype == 'Progress') {
					/* Project Monitoring and Control */
					identifier += '_PMC_' + milestone_short;
				}
				else if (momtype == 'Review') {
					/* QA Review */
					if (qa_review_skills.indexOf(skill) >= 0) {
						/* Review on a QA skill */
						identifier += '_REV_YY-xxx';
					}
					else {
						/* Review on other skill */
						identifier += '_Review_' + milestonetag_short;
					}
				}
				else if (momtype == 'Audit') {
					/* QA Audit */
					identifier += '_AUD_YY-xxx';
				}
			}
			else if (/^(?:AI|RISK)$/.test(ticket_type)) {
				identifier += '_xxxx';
			}
		}
	}
	else if (ticket_type == "DOC") {
		var configurationitem = configurationitem_get();
		var versionsuffix = versionsuffix_get();
		identifier = 'DOC_' + ((configurationitem === null) ? '<Configuration Item>' : configurationitem) + ((versionsuffix == "") ? '<Version Suffix>' : versionsuffix);
	}
	else if (ticket_type == "ECM") {
		// New tickets are not legacy ECMs
		identifier = 'ECM_' + program_name;
		var fromecm = fromecm_get();
		var chrono;
		if (fromecm == 'New Technical Note') {

			chrono = "xxx";
		}
		else {
			var sections = fromecm.split('_');
			chrono = sections[sections.length - 2];
		}
		identifier += "_" + chrono;
		if (ecmtype_get() == 'Technical Note') {
			identifier += versionsuffix_get();
		}
	}
	$("#field-summary").val(identifier);
	return true;
}

function update_tag(urltracbrowse, program_name) {
	/* Clear tag field if incoherent */
	var tag_name = $('#field-document').val();
	var tag_template = program_name + '_' + skill_get();
	if (!skill_is_unmanaged(program_name, tag_name) && !tag_name.match(tag_template)) {
		/* Update tag url for EFR/ECR ticket type */
		$('[name=doc_CI_select]').click(function() {
			location.href = urltracbrowse;
		});
		$('[name=doc_CI_select]').prop('title', urltracbrowse);
		$('#field-document').val('');
	}
}

function on_milestone_change(ticket_type, program_name) {
	if (ticket_type == 'MOM' && momtype_get() == 'Progress') {
		update_ticket_identifier(ticket_type, program_name);
	}
}

function on_milestonetag_change(ticket_type, program_name) {
	if (ticket_type == 'MOM' && (momtype_get() == 'CCB' || momtype_get() == 'Review')) {
		update_ticket_identifier(ticket_type, program_name);
	}
}

function on_skill_change(ticket_type, program_name, urltracbrowse) {
	if (/^(?:EFR|ECR)$/.test(ticket_type)) {
		var skill = skill_get();
		if (skill != "") {
			update_tag(urltracbrowse, program_name);
			milestone_filter();
		}
	}
	else if (ticket_type == 'MOM') {
		var skill = skill_get();
		if (skill == "") {
    		jqAlert("No skill is defined !");
    		return false;
		}
		var momtype_options = document.getElementById('field-momtype').options;
		var momtype_len = momtype_options.length;
		if (program_name == 'QA') {
			if (qa_review_skills.indexOf(skill) >= 0) {
				/* QA review: only 'Review' MOM type enabled and set, other disabled */
				for (var i=0; i<momtype_len; i++) {
					var option = momtype_options[i];
					if (option.text == 'Review') {
						option.disabled = false;
						momtype_set(option.text);
					}
					else {
						option.disabled = true;
					}
				}
			}
			else {
				/* QA audit: only 'Audit' MOM type enabled, other disabled */
				for (var i=0; i<momtype_len; i++) {
					var option = momtype_options[i];
					if (option.text == 'Audit') {
						option.disabled = false;
						momtype_set(option.text);
					}
					else {
						option.disabled = true;
					}
				}
			}
		}
		else {
			/* All MOM Types available */
			for (var i=0; i<momtype_len; i++) {
				var option = momtype_options[i];
				option.disabled = false;
			}
		}
		milestonetag_show_hide(program_name);
		milestone_show_hide(program_name);
	}
	else if (ticket_type == 'AI') {
		milestone_filter();
	}
	else if (ticket_type == 'RISK') {
		milestone_filter();
	}
	else if (ticket_type == 'DOC') {
		set_overlay();
		$("#field-owner option:not(:selected)").prop('disabled', 'disabled');
		configuration_item_filter();
		milestone_filter();
		doc_ecr_mom_link();
	}
	update_ticket_identifier(ticket_type, program_name);
}

function on_momtype_click() {
	// saves current momtype index in case of select cancel
	prev_momtype_index = document.getElementById('field-momtype').selectedIndex;
}

function qmsref_show_hide() {
	// Hide or show QMS Reference field depending on MOM Type field value
	if ($("#field-momtype").val() == 'Audit') {
		// Ticket properties
		$("label[for=field-qmsref]").parent().show();
		$("input#field-qmsref").parent().show();
	}
	else {
		// Ticket properties
		$("label[for=field-qmsref]").parent().hide();
		$("input#field-qmsref").parent().hide();
	}
}

function on_momtype_change(ticket_type, program_name, urltracbrowse) {
	if (!update_ticket_identifier(ticket_type, program_name)) {
		document.getElementById('field-momtype').selectedIndex = prev_momtype_index;
	}
	milestonetag_show_hide(program_name);
	milestone_show_hide(program_name);
	qmsref_show_hide();
}

function on_duedate_change(ticket_type, program_name, id) {
	var duedate = document.getElementById('field-duedate').value;
	if (!date_is_valid(duedate)) {
		jqAlert("The date is invalid");
	}
}

function aitype_show_hide() {
	// Hide or show AI Type field depending on Activity field value
	if ($("#field-activity").val() == 'Risk Management') {
		// Ticket properties
		$("label[for=field-aitype]").parent().show();
		$("select#field-aitype").parent().show();
	}
	else {
		// Ticket properties
		$("label[for=field-aitype]").parent().hide();
		$("select#field-aitype").parent().hide();
	}
}

function on_activity_change() {
	aitype_show_hide();
}

function on_configurationitem_change(ticket_type, program_name) {
	changetype_show_hide(program_name);
	versionsuffix_show_hide(program_name);
	sourcetype_filter();
	on_changetype_change(ticket_type, program_name);
}

function on_sourcetype_change() {
	// Setup pdfsigned default value (new document) or associated pdfsigned value (existing document)
	pdfsigned_setup();
	// Setup independence default value (new document) or associated independence value (existing document)
	independence_setup();
	// Setup controlcategory default value (new document) or associated controlcategory value (existing document)
	controlcategory_filter();
	// Setup submittedfor default value (new document) or associated submittedfor value (existing document)
	submittedfor_filter();
}

function reset_changetype() {
	$("select#field-changetype option").remove();
	$.each(g_changetypes, function(i, item) {
		$("select#field-changetype").append($('<option>', {
			text: item[0],
			value: item[0],
			title: item[1]
		}));
	})
}

function changetype_show_hide(program_name) {
	// Set up Change Type field depending on Configuration Item field value
	reset_changetype();
	var configurationitem = configurationitem_get();
	if (configurationitem != null && skill_is_unmanaged(program_name, configurationitem)) {
		// Change Type
		$('select#field-changetype option').filter(':not([value="Version"])').remove();
	}
	else {
		// Change Type
		$('select#field-changetype option').filter('[value="Version"]').remove();
		var href = window.location.href;
		var match = href.match(/newticket/);
		if (match) {
			/* Set change type */
			var qsParm = parse_query_string();
			for (var i=0; i < qsParm.length; i++) {
				if (qsParm[i][0] == 'changetype') {
					var changetype = qsParm[i][1];
					if (changetype != "") {
						changetype_set(changetype);
					}
					break;
				}
			}
		}
	}
}

function versionsuffix_show_hide(program_name) {
	// Set up Version Suffix field depending on Configuration Item field value
	var configurationitem = configurationitem_get();
	if (configurationitem != null && skill_is_unmanaged(program_name, configurationitem)) {
		// Version Suffix
		$("#field-versionsuffix").prop("readonly", false);
		$("#field-versionsuffix").removeAttr("style");
	}
	else {
		// Version Suffix
		if ($("#field-versionsuffix").attr("readonly") != undefined) {
			// if not admin ...
			$("#field-versionsuffix").prop("readonly", true);
			$("#field-versionsuffix").css("background-color", "#f4f4f4");
		}
	}
}

function setup_change_urls(configuration_item) {
    var base_url = location.href.substring(0, location.href
	    .indexOf("newticket"));
    var url = base_url + 'admin/tags_mgmt/documents/' + configuration_item
	    + '?caller=txxx';
    // Url on document properties for changing source type
    $("#change_st").prop("href", url).prop("target", "_self");
    // Url on document properties for changing PDF signing
    $("#change_ps").prop("href", url).prop("target", "_self");
    // Url on document properties for changing independence
    $("#change_ip").prop("href", url).prop("target", "_self");
    // Url on document properties for changing control category
    $("#change_cc").prop("href", url).prop("target", "_self");
    // Url on document properties for changing submission criteria
    $("#change_sf").prop("href", url).prop("target", "_self");
}

function hide_change_urls() {
    // Url on document properties for changing source type
    $("#change_st").hide();
    // Url on document properties for changing PDF signing
    $("#change_ps").hide();
    // Url on document properties for changing independence
    $("#change_ip").hide();
    // Url on document properties for changing control category
    $("#change_cc").hide();
    // Url on document properties for changing submission criteria
    $("#change_sf").hide();
}

function show_change_urls() {
    // Url on document properties for changing source type
    $("#change_st").show();
    // Url on document properties for changing PDF signing
    $("#change_ps").show();
    // Url on document properties for changing independence
    $("#change_ip").show();
    // Url on document properties for changing control category
    $("#change_cc").show();
    // Url on document properties for changing submission criteria
    $("#change_sf").show();
}

function disable_checkboxes() {
	/* ****** Lock pdfsigned checkbox *******/
	// Removes click event handlers - see enable_checkboxes()
	$('#field-pdfsigned').off('click');
	// Add new event handler
	// This is for disabling click event
	$('#field-pdfsigned').click(function(ev){
    	ev.preventDefault();});
	/* ****** Lock independence checkbox *******/
	// Removes click event handlers - see enable_checkboxes()
	$('#field-independence').off('click');
	// Add new event handler
	// This is for disabling click event
	$('#field-independence').click(function(ev){
    	ev.preventDefault();});
}

function enable_checkboxes() {
	/* ****** Unlock pdfsigned checkbox *******/
	// Removes click event handlers - see disable_checkboxes()
	$('#field-pdfsigned').off('click');
	// Add new event handler
	// This for handling an unchecked box whose value is not transmitted
	// In that case, and only in that case, value is gotten from hidden checkbox field
	// See trac/ticket/model.py
	$('#field-pdfsigned').click(function(ev){
		let val = pdfsigned_get() ? "1" : "0";
		$('#field-pdfsigned').val(val);
		$('#field-checkbox-pdfsigned').val(val);
	});
	/* ****** Unlock independence checkbox ****** */
	// Removes click event handlers - see disable_checkboxes()
	$('#field-independence').off('click');
	// Add new event handler
	// This for handling an unchecked box whose value is not transmitted
	// In that case, and only in that case, value is gotten from hidden checkbox field
	// See trac/ticket/model.py
	$('#field-independence').click(function(ev){
		let val = independence_get() ? "1" : "0";
		$('#field-independence').val(val);
		$('#field-checkbox-independence').val(val);
	});
}

function inputToSelect(id, name, fieldname) {
	field = $("#" + id);
	field.replaceWith($('<select id="' + id + '" name="' + name + '" class="tooltip"></select>'));
	// field still refers to the element that has been removed from the DOM,
	// not the new element that has replaced it.
	// see http://api.jquery.com/replacewith/
	field = $("#" + id);
	field.on("change", function() {
		window["on_" + fieldname + "_change"](g_ticket_type, g_program_name);
	});
	$("#propertyform").autoSubmit({preview: '1'}, function(data, reply) {
		$('#ticket').replaceWith(reply);
	  }, "#ticket .trac-loading");
	field.tooltipster({
		theme: 'tooltipster-noir',
		contentCloning: true,
		multiple: true,
		interactive: true
	});
	return field;
}

function selectToInput(id, name, fieldname) {
	field = $("#" + id);
	field.replaceWith($('<input type="text" id="' + id + '" name="' + name + '" style="min-width: 160px;" class="tooltip"></input>'));
	// field still refers to the element that has been removed from the DOM,
	// not the new element that has replaced it.
	// see http://api.jquery.com/replacewith/
	field = $("#" + id);
	$("#propertyform").autoSubmit({preview: '1'}, function(data, reply) {
		$('#ticket').replaceWith(reply);
	  }, "#ticket .trac-loading");
	field.tooltipster({
		theme: 'tooltipster-noir',
		contentCloning: true,
		multiple: true,
		interactive: true
	});
	return field;		
}

function applySelectValues(id, values) {
	field = $("#" + id);
	field.find("option").remove();
	$.each(values, function(i, item) {
		field.append($('<option>', {
			text: item,
			value: item,
			style: "min-width:160px"
		}));
	});
	field.trigger({type: 'keypress', which: 13, keyCode: 13});
	return field;		
}

function applyInputValue(id, values) {
	field = $("#" + id);
	if (values[0] !== undefined) {
		field.val(values[0]);
	}
	else {
		field.val("");
	}
	field.trigger({type: 'keypress', which: 13, keyCode: 13});
	field.prop("readonly", true);
	return field;
}

function artus_reqListener(status, fieldname, fieldvalue) {
	if (fieldname == "configurationitem") {
		$("#field-configurationitem optgroup").remove();
		$("#field-configurationitem option").remove();
		$.each(JSON.parse(fieldvalue), function(key, group) {
			var branch = $('<optgroup>', {label: key});

			$.each(group, function(i, item) {
				branch.append($("<option>", {
					text: item[0],
					value: item[0],
					selected: item[1],
					style: "min-width:160px"
				}));
			});

			$("#field-configurationitem").append(branch);
		})
		on_configurationitem_change(g_ticket_type, g_program_name);
	}
	else if (fieldname == "milestone") {
		$("#field-milestone optgroup").remove();
		$("#field-milestone option").remove();
		$.each(JSON.parse(fieldvalue), function(i, item) {
			$("#field-milestone").append($('<option>', {
				text: item[0],
				value: item[0],
				selected: item[1],
				style: "min-width:160px"
			}));
		});
		on_milestone_change(g_ticket_type, g_program_name);
	}
	else if (fieldname == "milestonetag") {
		$("#field-milestonetag optgroup").remove();
		$("#field-milestonetag option").remove();
		$.each(JSON.parse(fieldvalue), function(key, group) {
			var milestone = $('<optgroup>', {label: key});

			$.each(group, function(i, item) {
				milestone.append($("<option>", {
					text: item,
					value: item,
					style: "min-width:160px"
				}));
			});

			$("#field-milestonetag").append(milestone);
		})
		var qsParm = parse_query_string();
		for (var i=0; i < qsParm.length; i++) {
			if (qsParm[i][0] == 'milestonetag') {
				var milestonetag = qsParm[i][1];
				break;
			}
		}
		if (typeof(milestonetag) != "undefined") {
			$('#field-milestonetag option[value="' + milestonetag + '"]').prop("selected", true);
		}
		else {
			$("#field-milestonetag option:first").prop("selected", true);
		}
		on_milestonetag_change(g_ticket_type, g_program_name);
	}
	else if (fieldname == "fromversion") {
		$("#field-fromversion option").remove();
		$.each(JSON.parse(fieldvalue), function(i, item) {
			$("#field-fromversion").append($('<option>', {
				text: item[0],
				value: item[0],
				selected: item[1],
				style: "min-width:160px"
			}));
		});
		on_fromversion_change(g_ticket_type, g_program_name);
	}
	else if (fieldname == "versionsuffix") {
		versionsuffix_set(JSON.parse(fieldvalue));
		on_versionsuffix_change(g_ticket_type, g_program_name);
	}
	else if (fieldname == "fromecm") {
		$("#field-fromecm option").remove();
		$.each(JSON.parse(fieldvalue), function(i, item) {
			$("#field-fromecm").append($('<option>', {
				text: item[0],
				value: item[0],
				selected: item[1],
				style: "min-width:160px"
			}));
		});
		on_fromecm_change(g_ticket_type, g_program_name);
	}
	else if (fieldname == "fromfee") {
		$("#field-fromfee option").remove();
		$.each(JSON.parse(fieldvalue), function(i, item) {
			$("#field-fromfee").append($('<option>', {
				text: item[0],
				value: item[0],
				selected: item[1],
				style: "min-width:160px"
			}));
		});
		unset_overlay();
		on_fromfee_change(g_ticket_type, g_program_name);
	}
	else if (fieldname == "evolref") {
		$("#field-evolref option").remove();
		$.each(JSON.parse(fieldvalue), function(i, item) {
			$("#field-evolref").append($('<option>', {
				text: item,
				value: item,
				style: "min-width:160px"
			}));
		});
		unset_overlay();
		on_evolref_change(g_ticket_type, g_program_name);
	}
	else if (["customer", "program", "application"].includes(fieldname)) {
		let id = "field-" + fieldname;
		let name = "field_" + fieldname;
		let field = $("#" + id);
		let values = JSON.parse(fieldvalue);
		if (values.length > 1) {
			if (field.is("input")) {
				inputToSelect(id, name, fieldname);
			}
			applySelectValues(id, values);
		}
		else {
			if (field.is("select")) {
				selectToInput(id, name, fieldname);
			}
			applyInputValue(id, values);
		}
		unset_overlay();
		if (fieldname == "customer") {
			program_filter();
		}
		else if(fieldname == "program") {
			application_filter();
		}
		else if (fieldname =="application") {
			itemsdisplay_filter();
		}
	}
	else if (fieldname == "itemsdisplay") {
		data = JSON.parse(fieldvalue);
		itemsdisplay_set(data);
		$("#propertyform").autoSubmit({preview: '1'}, function(data, reply) {
			$('#ticket').replaceWith(reply);
		  }, "#ticket .trac-loading");
		unset_overlay();
	}
	else if (fieldname == "keywords") {
		keywords_set(JSON.parse(fieldvalue));
	}
	else if (fieldname == "distribution") {
		let data = JSON.parse(fieldvalue);
		toname_set(data['toname']);
		toemail_set(data['toemail']);
		tophone_set(data['tophone']);
	}
	else if (fieldname == "sourcetype") {
		// Setup sourcetype list and default value (new document) or associated sourcetype (existing document)
		let $el = $("select#field-sourcetype");
		$el.empty();
		$.each(JSON.parse(fieldvalue), function(key, group) {
			var software = $('<optgroup>', {label: key});

			$.each(group, function(i, item) {
				software.append($("<option>", {
					text: item[0],
					value: key + ":" + item[0],
					selected : item[1],
					title : item[2]
				}));
			});

			$el.append(software);
		})

		/* Set source type selected value if match found with configuration item*/
		var qsParm = parse_query_string();
		for (var i=0; i < qsParm.length; i++) {
			if (qsParm[i][0] == 'sourcetype') {
				let sourcetype = qsParm[i][1];
				if (sourcetype != "") {
					sourcetype_set(sourcetype);
				}
				break;
			}
		}
		// Setup other dependent values
		on_sourcetype_change();
	}
	else if (fieldname == "pdfsigned") {
		let data = JSON.parse(fieldvalue);
		pdfsigned_set(data == 'true');
	}
	else if (fieldname == "independence") {
		let data = JSON.parse(fieldvalue);
		independence_set(data == 'true');
	}
	else if (fieldname == "controlcategory") {
		var $el = $("select#field-controlcategory");
		$el.empty();
		$.each(JSON.parse(fieldvalue), function(i, item) {
			$el.append($('<option>', {
				text: item[0],
				value: item[0],
				selected: item[1],
				title: item[2]
			}));
		});
		unset_overlay();
	}
	else if (fieldname == "submittedfor") {
		var $el = $("select#field-submittedfor");
		$el.empty();
		$.each(JSON.parse(fieldvalue), function(i, item) {
			$el.append($('<option>', {
				text: item[0],
				value: item[0],
				selected: item[1],
				title: item[2]
			}));
		});
		unset_overlay();
	}
	else if (fieldname == "edit-doc-file") {
		fieldvalue = JSON.parse(fieldvalue);
		if (fieldvalue == "lock") {
			// Change lock/unlock status
			if (typeof g_doc_lock != 'undefined') {
				g_doc_lock = true;
			}
			// Change 'View' button into 'Edit' button
			$('input#source_edit').prop('value', 'Edit').prop('title', 'Edit the source file (working copy on the Trac server)');
			unset_focus('lock');
			unset_focus('unlock');
			set_focus('edit');
			// Disable source and pdf file selector
			$('select#field-sourcefile').prop('disabled', true);
			$('select#field-pdffile').prop('disabled', true);
			// Disable View eyes
			$('a#sourcefile').click(function(){return false;});
			$('a#sourcefile > img').css('opacity', 0.5);
			$('a#sourcefile > img').prop('title', 'Disabled because locked');
			$('a#pdffile').click(function(){return false;});
			$('a#pdffile > img').css('opacity', 0.5);
			$('a#pdffile > img').prop('title', 'Disabled because locked');
			// Background color
			$('fieldset[name=working-copy]').css({'background-color': '#F4FFF4',
												  'box-shadow': 'none'});
			// Update workflow
			setup_workflow();
			// Buttons disabled
			UIComponents.buttons.CreateTicketSubmitChanges.object.disable();
			$('input[value="Browse"]').prop('disabled', true);
	        // Re-apply locks on ticket header
	        ticket_header_doc_locker();
			unset_overlay();
		}
		else if (fieldvalue == "wait_unlock") {
			// Changes committed
			unset_src_modified();
			unset_pdf_modified();
			// Change lock/unlock status
			if (typeof g_doc_lock != 'undefined') {
				g_doc_lock = false;
	    		unset_beforeunload_event_handler();
			}
			// Change 'Edit' button into 'View' button
			$('input#source_edit').prop('value', 'View').prop('title', 'View the source file (working copy on the Trac server)');
			set_focus('lock');
			unset_focus('unlock');
			unset_focus('edit');
			// Enable source and pdf file selector
			$('select#field-sourcefile').prop('disabled', false);
			$('select#field-pdffile').prop('disabled', false);
			// Take care of View eyes
			on_sourcefile_change();
			on_pdffile_change();
			// Background color
			$('fieldset[name=working-copy]').css({'background-color': '',
												  'box-shadow': ''});
			// Update workflow
			setup_workflow();
			// Buttons enabled
			UIComponents.buttons.CreateTicketSubmitChanges.object.enable();
			$('input[value="Browse"]').prop('disabled', false);
			// Source url update
			var data = {};
			data.ticket_id = ticketid_get();
			let async = true;
			artus_xhr("sourceurl", data, async, "GET");
		}
	}
	else if (fieldname == "sourcefile") {
		fieldvalue = JSON.parse(fieldvalue);
		if (fieldvalue == true) {
			set_edition_mode();
		}
	}
	else if (fieldname == "sourceurl") {
		// Update ticket box and properties following commit
		let data = JSON.parse(fieldvalue);
		let sourceurl = data['sourceurl'];
		let revision = sourceurl.match(/rev=(\d+)/)[1];
		// Sourceurl update (ticket box)
		$("th#h_sourceurl ~ td>a").html(sourceurl);
		// Sourceurl update (ticket properties)
		$("input#field-sourceurl").val(sourceurl);
		// Sourceurl browse button title and onclick update (ticket properties) - if visible
		let element = $("input[name=sourceurl_browse]");
		if (element.length)
		{
			let browsebutton_title = element.attr('title');
			browsebutton_title = browsebutton_title.replace(/rev=\d+/, "rev=" + revision);
			element.attr('title', browsebutton_title);
			let browsebutton_onclick = element.attr('onclick');
			browsebutton_onclick = browsebutton_onclick.replace(/rev=\d+/, "rev=" + revision);
			element.attr('onclick', browsebutton_onclick);
		}
		// Source file link update (ticket box)
		let sourcefile_a = $("td[headers=h_sourcefile]>a");
		if (sourcefile_a.length) {
			let sourcefile_href = sourcefile_a.prop('href').replace(/([?&]rev=)[^&]+/,  '$1' + revision);
			$("td[headers=h_sourcefile]>a").prop('href', sourcefile_href).prop('target', '_self');
		}
		// PDF file link update (ticket box)
		let pdffile_a = $("td[headers=h_pdffile]>a");
		if (pdffile_a.length) {
			let pdffile_href = pdffile_a.prop('href').replace(/([?&]rev=)[^&]+/,  '$1' + revision);
			$("td[headers=h_pdffile]>a").prop('href', pdffile_href).prop('target', '_self');
		}
		// Update view time
		let view_time = data['view_time'];
		$("#propertyform input[name='view_time']").val(view_time);
        // Re-apply locks on ticket header
        ticket_header_doc_locker();
		unset_overlay();
	}
	else if(fieldname == "view_time") {
		// Update view time
		let view_time = JSON.parse(fieldvalue);
		$("#propertyform input[name='view_time']").val(view_time);
		unset_overlay();
	}
	else if (fieldname == "src_wc_status" || fieldname == "pdf_wc_status") {
		let wc_status = JSON.parse(fieldvalue);
		switch(wc_status) {
			case 'ticket not in edition':
				jqAlert("The ticket is not in edition mode");
				break;
			case 'update required':
				jqAlert("Update the sourceurl by refreshing the ticket");
				break;
			case 'modified':
				if (fieldname == "src_wc_status") {
					set_src_modified();
					unset_focus('lock');
					set_focus('unlock');
					unset_focus('edit');
				}
				else {
					set_pdf_modified();
				}
				break;
			default:
				if (fieldname == "src_wc_status") {
					unset_src_modified();
				}
				else {
					unset_pdf_modified();
				}
		}
	}
	else if (fieldname == "sourcefile_url") {
		let data = JSON.parse(fieldvalue);
		let href = data['href'];
		let comment = data['comment'];
		// Update View eye
		$('a#sourcefile').prop('href', href);
		if (comment == 'N/A') {
			// Disable View eye if N/A
			$('a#sourcefile').on('click', false);
			$('a#sourcefile > img').css('opacity', 0.5);
			$('a#sourcefile > img').prop('title', 'Disabled because N/A');
		}
		else if (comment == 'template folder') {
			// Enable View eye
			$('a#sourcefile').off('click');
			$('a#sourcefile > img').css('opacity', 1.0);
			$('a#sourcefile > img').prop('title', 'View File (template folder on the Trac server)');
		}
		else if (comment == 'document folder') {
			if (typeof g_doc_lock != 'undefined' && g_doc_lock == true) {
				// Disable View eye if locked
				$('a#sourcefile').on('click', false);
				$('a#sourcefile > img').css('opacity', 0.5);
				$('a#sourcefile > img').prop('title', 'Disabled because locked');
			}
			else {
				// Enable View eye
				$('a#sourcefile').off('click');
				$('a#sourcefile > img').css('opacity', 1.0);
				$('a#sourcefile > img').prop('title', 'View File (document folder in the repository on the Trac server)');
			}
		}
	}
	else if (fieldname == "pdffile_url") {
		let data = JSON.parse(fieldvalue);
		let href = data['href'];
		let comment = data['comment'];
		// Update View eye
		$('a#pdffile').prop('href', href);
		if (comment == 'N/A') {
			// Disable View eye if N/A
			$('a#pdffile').on('click', false);
			$('a#pdffile > img').css('opacity', 0.5);
			$('a#pdffile > img').prop('title', 'Disabled because N/A');
		}
		else if (comment == 'does not exist') {
			// Disable View eye if file does not exist
			$('a#pdffile').on('click', false);
			$('a#pdffile > img').css('opacity', 0.5);
			$('a#pdffile > img').prop('title', 'Disabled because file does not exist');
		}
		else if (comment == 'empty') {
			// Disable View button if file is empty
			$('input[name=pdf_view]').prop('disabled', true);
			$('input[name=pdf_view]').prop('title', 'Disabled because file is empty');
			// Disable View eye if file is empty
			$('a#pdffile').on('click', false);
			$('a#pdffile > img').css('opacity', 0.5);
			$('a#pdffile > img').prop('title', 'Disabled because file is empty');
			// Disable ticket box link if file is empty
			$('a#pdffile_link').click(function(e) {
				e.preventDefault();
			});
			$('a#pdffile_link').prop('title', 'Disabled because file is empty');
		}
		else if (comment == 'document folder') {
			if (typeof g_doc_lock != 'undefined' && g_doc_lock == true) {
				// Disable View eye if locked
				$('a#pdffile').on('click', false);
				$('a#pdffile > img').css('opacity', 0.5);
				$('a#pdffile > img').prop('title', 'Disabled because locked');
			}
			else {
				// Enable View eye
				$('a#pdffile').off('click');
				$('a#pdffile > img').css('opacity', 1.0);
				$('a#pdffile > img').prop('title', 'View File (document folder in the repository on the Trac server)');
			}
			$('a#pdffile_link').off('click');
			$('a#pdffile_link').prop('title', 'View File (document folder in the repository on the Trac server)');
			$("input[name=pdf_view]").prop('title', 'View the PDF file (working copy on the Trac server');
		}
	}
	else if (fieldname == "sourcefile_exists") {
		let exist = JSON.parse(fieldvalue);
		if (exist) {
			$("input[name=source_edit]").prop('disabled', false);
		}
		else {
			$("input[name=source_edit]").prop('disabled', true);
			$("input[name=lock_unlock]").prop('disabled', true);
		}
	}
	else if (fieldname == "pdffile_exists") {
		let exist = JSON.parse(fieldvalue);
		if (exist) {
			$("input[name=pdf_view]").prop('disabled', false);
		}
		else {
			$("input[name=pdf_view]").prop('disabled', true);
		}
	}
	else if (fieldname == "src_locker") {
		let sourcefile_a = $("td[headers=h_sourcefile]>a");
		if (sourcefile_a.length) {
			if (sourcefile_a.text() != "N/A") {
				$("td[headers=h_sourcefile]>sub[id=src_lock_image]").remove();
				let locker = JSON.parse(fieldvalue);
				if (locker.startsWith("Locked")) {
					$("td[headers=h_sourcefile]").append('<sub id="src_lock_image"><img src="/htdocs/lock12.png" title="' + locker + '"></img></sub>');
				}
				else {
					$("td[headers=h_sourcefile]").append('<sub id="src_lock_image"><img src="/htdocs/unlock12.png" title="' + locker + '"></img></sub>');
				}
			}
		}
	}
	else if (fieldname == "pdf_locker") {
		let pdffile_a = $("td[headers=h_pdffile]>a");
		if (pdffile_a.length) {
			if (pdffile_a.text() != "N/A") {
				$("td[headers=h_pdffile]>sub[id=pdf_lock_image]").remove();
				let locker = JSON.parse(fieldvalue);
				if (locker.startsWith("Locked")) {
					$("td[headers=h_pdffile]").append('<sub id="pdf_lock_image"><img src="/htdocs/lock12.png" title="' + locker + '"></img></sub>');
				}
				else {
					$("td[headers=h_pdffile]").append('<sub id="pdf_lock_image"><img src="/htdocs/unlock12.png" title="' + locker + '"></img></sub>');
				}
			}
		}
	}
	else if (fieldname == "src_repos_status" || fieldname == "pdf_repos_status") {
		let repos_status = JSON.parse(fieldvalue);
		switch(repos_status) {
			case 'ticket closed':
				jqAlert("The ticket is closed");
				break;
			case 'update required':
				jqAlert("Update the sourceurl by refreshing the ticket");
				break;
			case '':
				if (fieldname == "src_repos_status") {
					check_external_lock_on_pdf();
				}
				else {
					lock_files();
				}
				break;
			default:
				if (fieldname == "src_repos_status") {
					jqAlert("The source file is locked by " + repos_status + " - see the ticket header for more info. Remove that lock in order to lock the online working copy.");
				}
				else {
					jqAlert("The pdf file is locked by " + repos_status + " - see the ticket header for more info. Remove that lock in order to lock the online working copy.");
				}
		}
		if (repos_status) {
			// Undo lock action
			$("input#lock").prop('checked', false);
			$("input#unlock").prop('checked', true);
			unset_overlay();
		}
	}
	else if (fieldname == "lock_unlock_description") {
		let data = JSON.parse(fieldvalue);
		$('#lock_description').prop('title', data['lock_description']);
		$('#unlock_description').prop('title', data['unlock_description']);
	}
	else if (fieldname == "mom_lock_status") {
		let lock_status = JSON.parse(fieldvalue);
		let location = '/tracs/' + g_trac_env_name + '/ticket/' + g_trac_id;
		if (gup("merged") != "") {
			location += '?merged=True';
		}
		if (document.getElementById('force-edit-mode') != null) {
			if ((document.getElementById('force-edit-mode').checked || (gup("forced") != "")) && lock_status == null) {
				document.location = location;
				set_overlay();
			}
			else {
				if (!(document.getElementById('force-edit-mode').checked || (gup("forced") != "")) && lock_status != null) {
					if (gup("merged") != "") {
						document.location = location + '&forced=True';
					}
					else {
						document.location = location + '?forced=True';
					}
					set_overlay();
				}
			}
		}
	}
	else if (fieldname == "workflow") {
		let data = JSON.parse(fieldvalue);
		let activities = data['activities'];
		let allowed_actions = data['allowed_actions'];
		$('#current_activity').text(activities[g_ticket_status]);
		$('#current_status').text(g_ticket_status);
		// Enable or Disable work flow actions
		let action;
		let action_enabled;
		let first_enabled_action;
		for (action in allowed_actions)
		{
			action_enabled = allowed_actions[action][0];
			$("input#action_" + action).prop("disabled", !action_enabled).prop("title", allowed_actions[action][1]);
			if (action_enabled && first_enabled_action === undefined) {
				first_enabled_action = action
			}
		}
		// Select first enabled workflow action (unless in case of warnings)
		if ($('div#warning').length == 0) {
			if (first_enabled_action === undefined)
			{
				$("input[id^=action_]:checked").prop('checked', false).trigger('click');
				UIComponents.buttons.CreateTicketSubmitChanges.object.disable();
				$('input[value="Browse"]').prop('disabled', true);
			}
			else {
				$("input#action_" + first_enabled_action).prop('checked', true).trigger('click');
			}
		}
	}
	else if (fieldname == "change_history") {
		let div = $("#change_history");
		if (!div.has("#changelog").length){
			$("#change_history").append(fieldvalue);
		}
		else {
			$("#changelog").replaceWith(fieldvalue);
		}
		unset_overlay();
	}
	else if (fieldname == "change_comment") {
		change_history();
	}
	else if (fieldname == "ecr_mom_report_url") {
		let data = JSON.parse(fieldvalue);
		let ecr_report_url = data['ecr_report_url'];
		let mom_report_url = data['mom_report_url'];
		let skill = data['skill'];
		$('a#ecr_report_url').remove();
		$('input#field-blocking').after('<a id="ecr_report_url" href="' + ecr_report_url + '" title="View all open ECRs of skill ' + skill + '" class="tooltip" target="_blank"><img src="/htdocs/eye.png"></img></a>');
		$('a#mom_report_url').remove();
		$('input#field-parent').after('<a id="mom_report_url" href="' + mom_report_url + '" title="View all open CCB MOMs of skill ' + skill + ' for the selected milestone" class="tooltip" target="_blank"><img src="/htdocs/eye.png"></img></a>');
	}
	else if (fieldname == "pre-fill_mom") {
		unset_overlay();
	}
}

function artus_xhr(fieldname, data, async, method) {
	var project_url = this.location.protocol + "//" + this.location.host + "/tracs/" + g_trac_env_name;
	let url;
	if (method == "GET") {
		data.field = fieldname;
		url = project_url + "/xhrget";
	}
	else {  // POST
		if (typeof g_trac_id != 'undefined') data.ticket_id = g_trac_id;
		url = project_url + "/xhrpost";
	}
	$.ajax({
	    url : url,
	    data: data,
	    async : async,
	    method : method,
	    contentType : false,
	    success : function(data, textStatus, jqXHR){
	        artus_reqListener(jqXHR.status, fieldname, jqXHR.responseText);
	    },
	    error : function(jqXHR, textStatus, errorThrown){
	    	let qs_params = parse_query_string(this.data);
	    	let action;
	    	for (let i=0; i < qs_params.length; i++) {
	    		if (qs_params[i][0] == 'action') {
	    			action = qs_params[i][1];
	    			break;
	    		}
	    	}
	    	// Cancel server side ongoing actions when unloading or aborting the page
	        if (!jqXHR.getAllResponseHeaders()) {
				if (action == "wait_unlock") {
					let url = project_url + "/beacon";
					if (typeof g_trac_id != 'undefined') {
						let searchParams = new URLSearchParams("ticket_id=" + g_trac_id);
						navigator.sendBeacon(url, searchParams);
					}
				}
	        }
	        else {
		        // Report error on screen
		    	unset_overlay();
		    	let outputMsg = jqXHR.responseText;
		    	let titleMsg = textStatus + ' : ' + errorThrown;
			    titleMsg += ' - A screenshot may help the Trac support team !';
		    	titleMsg += ' - Type <ESC> to close this window.';
		    	jqAlert(outputMsg, titleMsg, function() {
		    		location.reload();
		    	});
	        }
	    }
	});
}

function on_changetype_change(ticket_type, program_name) {
	fromversion_filter();
	if (changetype_get() == 'Status') {
		if (fromversion_get() == null) {
			jqAlert("Change Type cannot be set to Status because no From Version was found that is not already Released and not already associated to a DOC ticket.");
			// Defaults back to Edition if empty From Version
			changetype_set('Edition');
			fromversion_filter();
		}
	}
}

function on_fromversion_change(ticket_type, program_name) {
	let changetype = changetype_get();
	let fromversion = fromversion_get();
	let configurationitem = configurationitem_get();
	if (changetype == 'Version') {
		if (skill_is_unmanaged(program_name, fromversion)) {
			versionsuffix_set("");
		}
	}
	else {
		if (fromversion == 'New Document' || fromversion.startsWith('New Branch Document') || fromversion == null) {
			if (configurationitem != null && changetype != 'Status' && configurationitem != "") {
				if (fromversion == 'New Document') {
					versionsuffix_set('_1.0');
				}
				else if (fromversion.startsWith('New Branch Document')) {
					if (g_branch_segregation_activated) {
						let branch = branch_get();
						let branch_no = parseInt(branch.substring(1));
						let first_branch_no = parseInt(g_branch_segregation_first_branch.substring(1));
						let standard = branch_no - first_branch_no + 1;
						versionsuffix_set('_' + standard + '.1.0');
					}
					else {
						versionsuffix_set('_1.0');
					}
				}
				$('select#field-changetype option').filter(':not([value="Edition"])').remove();
			}
			else {
				versionsuffix_set("");
			}
			on_versionsuffix_change(g_ticket_type, g_program_name);
		}
		else {
			versionsuffix_filter();
		}
	}
	if (configurationitem == null || fromversion_get() == 'New Document') {
		hide_change_urls();
		enable_checkboxes();
	} else {
		setup_change_urls(configurationitem);
		show_change_urls();
		disable_checkboxes();
	}
}

function on_fromecm_change(ticket_type, program_name) {
	var fromecm;
	fromecm = fromecm_get();
	if (fromecm == 'New Technical Note') {
		versionsuffix_set("_v1");
		keywords_set('');
	}
	else {
		var index = parseInt(fromecm.split('_').pop().slice(1)) + 1;
		versionsuffix_set("_v" + index);
		keywords_filter();
	}
	on_versionsuffix_change(ticket_type, program_name);
}

function on_fromfee_change(ticket_type, program_name) {
	var fromfee;
	fromfee = fromfee_get();
	if (fromfee == 'New Evolution Sheet') {
		versionsuffix_set("_v1");
	}
	else {
		var index = parseInt(fromfee.split('_').pop().slice(1)) + 1;
		versionsuffix_set("_v" + index);
	}
	on_versionsuffix_change(ticket_type, program_name);
}

function on_evolref_change(ticket_type, program_name) {
	// Set ticket ID
	identifier = 'FEE_' + program_name;
	identifier += "_" + evolref_get();
	identifier += versionsuffix_get();
	$("#field-summary").val(identifier);
	customer_filter();
}

function on_customer_change(ticket_type, program_name) {
	program_filter();
}

function on_program_change(ticket_type, program_name) {
	application_filter();
}

function on_application_change(ticket_type, program_name) {
}

function on_versionsuffix_change(ticket_type, program_name) {
	if (ticket_type == 'ECM' || ticket_type == 'DOC') {
		update_ticket_identifier(ticket_type, program_name);
	}
	else if (ticket_type == 'FEE') {
		// Get evolref(s)
		evolref_filter();
	}
}

function milestonetag_show_hide(program_name) {
	// Hide or show Milestone Tag field depending on Ticket Type and Skill values
	var skill = skill_get();
	var momtype = momtype_get();
	if (g_ticket_type == 'MOM' && (momtype == 'Review' || momtype == 'CCB') && qa_review_skills.indexOf(skill) == -1) {
		// Shown only for MOM Review/CCB and not QA skill
		milestonetag_filter();
		// Ticket properties
		$("label[for=field-milestonetag]").show();
		$("select#field-milestonetag").show();
		// required for sending input value when showed
		$("select#field-milestonetag").prop("disabled", false);
		$("span#explain-milestonetag").show();
	}
	else {
		// Hide for all others
		// Ticket properties
		$("label[for=field-milestonetag]").hide();
		$("select#field-milestonetag").hide();
		// required for not sending input value when showed
		$("select#field-milestonetag").prop("disabled", true);
		$("span#explain-milestonetag").hide();
	}
}

function milestone_show_hide(program_name) {
	// Hide or show Milestone field depending on Ticket Type, Skill and MOM type values
	var skill = skill_get();
	var momtype = momtype_get();
	if (g_ticket_type == 'MOM' && momtype == 'Progress' && typeof g_ticket_status == "undefined") {
		// Shown only for non created MOM Progress ticket
		milestone_filter();
		// Ticket properties
		$("label[for=field-milestone]").show();
		$("select#field-milestone").show();
	}
	else {
		// Hide for all others
		// Ticket properties
		$("label[for=field-milestone]").hide();
		$("select#field-milestone").hide();
	}
}

function setup_workflow(force_reassign) {
	var data = {};
	data.ticket_id = ticketid_get();
	if (typeof(force_reassign) != 'undefined') {
		data.force_reassign = force_reassign;
	}
	if (typeof(g_doc_sourcefile_status) != 'undefined') {
		data.version_status = g_doc_sourcefile_status;
	}
	if (typeof(g_mom_lock) != 'undefined') {
		data.mom_locked = g_mom_lock;
	}
	var async = true;
	artus_xhr("workflow", data, async, "GET");
}

function set_lock_unlock_description(src_file, pdf_file) {
	var data = {};
	data.ticket_id = ticketid_get();
	data.src_file = src_file;
	data.pdf_file = pdf_file;
	var async = true;
	artus_xhr("lock_unlock_description", data, async, "GET");
}

function set_mom_lock_status() {
	var data = {};
	data.ticket_id = ticketid_get();
	var async = true;
	artus_xhr("mom_lock_status", data, async, "GET");
}

function change_history(cnum, version) {
	var data = {};
	data.ticket_id = ticketid_get();
	if (arguments.length == 0) {
		// Change history button
	}
	else if (arguments.length == 1) {
		// Edit button
		data.cnum_edit = cnum;
	}
	else if (arguments.length == 2) {
		if (version == '-1') {
			/* Preview button */
			var comment = $("div#changelog div[id^=trac-change-" + cnum + "] textarea[name=edited_comment]").val();
			data.cnum_edit = cnum;
			data.edited_comment = comment;
		}
		else {
			// Previous/Next button
			data.cnum_hist = cnum;
			data.cversion = version;
		}
	}
	var async = true;
	artus_xhr("change_history", data, async, "GET");
	set_overlay();
}

function change_comment_edit(cnum) {
	// Submit changes button
	var comment = $("div#changelog div[id^=trac-change-" + cnum + "-] textarea[name=edited_comment]").val();
	var data = {};
	data.action = "change_comment_edit";
	data.cnum_edit = cnum;
	data.edited_comment = comment;
	var async = true;
	artus_xhr("change_comment", data, async, "POST");
	set_overlay();
}

function milestone_filter() {
	// Filter milestones for EFR/ECR/DOC/MOM/AI/RISK
	// according to skill
	var data = {};
	data.skill = skill_get();
	data.milestone = milestone_get();
	var async = true;
	artus_xhr("milestone", data, async, "GET");
}

function milestonetag_filter() {
	// Filter milestonetags for MOM CCB/Review
	// according to skill && MOM type
	var data = {};
	data.skill = skill_get();
	data.momtype = momtype_get();
	var async = true;
	artus_xhr("milestonetag", data, async, "GET");
}

function configuration_item_filter() {
	// Filter configuration items for DOC
	// according to skill
	var data = {};
	data.skill = skill_get();
	var qs_params = parse_query_string();
	for (var i=0; i < qs_params.length; i++) {
		if (qs_params[i][0] == 'configurationitem') {
			data.configurationitem = qs_params[i][1];
		}
		else if (qs_params[i][0] == 'sourceurl') {
			data.sourceurl = qs_params[i][1];
		}
	}
	if (qs_params.length == 0) {
		var configurationitem = configurationitem_get();
		if (configurationitem != null && configurationitem != "") {
			data.configurationitem = configurationitem;
		}
	}
	var async = false;
	artus_xhr("configurationitem", data, async, "GET");
}

function fromversion_filter() {
	// Filter from version for DOC
	// according to configuration item and change type
	var data = {};
	data.configurationitem = configurationitem_get();
	data.branch = branch_get();
	data.changetype = changetype_get();
	data.fromversion = fromversion_get();
	var async = false;
	artus_xhr("fromversion", data, async, "GET");
}

function versionsuffix_filter() {
	// Filter version suffix for DOC
	// according to from version and change type
	var data = {};
	data.changetype = changetype_get();
	data.fromversion = fromversion_get();
	var async = false;
	artus_xhr("versionsuffix", data, async, "GET");
}

function fromecm_filter() {
	// Filter from chrono for ECM
	var data = {};
	data.fromecm = fromecm_get();
	var async = true;
	artus_xhr("fromecm", data, async, "GET");
}

function fromfee_filter() {
	// Get previous FEE version from Trac database
	// unless new evolution sheet
	var data = {};
	data.fromfee = fromfee_get();
	var async = true;
	artus_xhr("fromfee", data, async, "GET");
	set_overlay();
}

function evolref_filter() {
	var fromfee = fromfee_get();
	if (fromfee == 'New Evolution Sheet') {
		// Get evol ref from a SQLServer View
		var data = {};
		data.fromfee = fromfee;
		var async = true;
		artus_xhr("evolref", data, async, "GET");
		set_overlay();
	}
	else {
		// Get evol ref from the selected previous FEE version
		var sections = fromfee.split('_');
		item = sections[sections.length - 2];
		$("#field-evolref option").remove();
		$("#field-evolref").append($('<option>', {
			text: item,
			value: item,
			style: "min-width:160px"
		}));
		on_evolref_change(g_ticket_type, g_program_name);
	}
}

function customer_filter() {
	// Get customer from the selected previous FEE version
	// or from a SQLServer View if a new evolution sheet
	var data = {};
	data.evolref = evolref_get();
	var async = true;
	artus_xhr("customer", data, async, "GET");
	set_overlay();
}

function program_filter() {
	// Get program from the selected previous FEE version
	// or from a SQLServer View if a new evolution sheet
	var data = {};
	data.evolref = evolref_get();
	data.customer = customer_get();
	var async = true;
	artus_xhr("program", data, async, "GET");
	set_overlay();
}

function application_filter() {
	// Get application from the selected previous FEE version
	// or from a SQLServer View if a new evolution sheet
	var data = {};
	data.evolref = evolref_get();
	data.customer = customer_get();
	data.program = program_get();
	var async = true;
	artus_xhr("application", data, async, "GET");
	set_overlay();
}

function itemsdisplay_filter() {
	// Get items from the selected previous FEE version
	// or from a SQLServer View if a new evolution sheet
	var data = {};
	data.evolref = evolref_get();
	data.customer = customer_get();
	data.program = program_get();
	data.application = application_get();
	var async = true;
	artus_xhr("itemsdisplay", data, async, "GET");
	set_overlay();
}

function keywords_filter() {
	// Fill in keywords from previous ECM Technical Note version
	var data = {};
	if (g_ticket_type == 'ECM') {
		data.summary = fromecm_get();
	}
	var async = true;
	artus_xhr("keywords", data, async, "GET");
}

function distribution_filter() {
	// Fill in toname, toemail, tophone from previous ECM Technical Note / FEE version
	var data = {};
	data.ticket_id = ticketid_get();
	var async = true;
	artus_xhr("distribution", data, async, "GET");
}

function sourcetype_filter() {
    // Filter source type for DOC
    // according to configuration item
    var data = {};
    data.configurationitem = configurationitem_get();
    var async = true;
    artus_xhr("sourcetype", data, async, "GET");
}

function independence_setup() {
    // Setup independence for DOC
    // according to configuration item
    var data = {};
    data.configurationitem = configurationitem_get();
    data.sourcetype = sourcetype_get();
    var async = true;
    artus_xhr("independence", data, async, "GET");
}

function pdfsigned_setup() {
    // Setup PDF signing for DOC
    // according to configuration item
    var data = {};
    data.configurationitem = configurationitem_get();
    data.sourcetype = sourcetype_get();
    var async = true;
    artus_xhr("pdfsigned", data, async, "GET");
}

function controlcategory_filter() {
	// Filter controlcategory for DOC
	// according to configuration item
	var data = {};
	data.configurationitem = configurationitem_get();
	data.sourcetype = sourcetype_get();
	var async = true;
	artus_xhr("controlcategory", data, async, "GET");
}

function submittedfor_filter() {
	// Filter submittedfor for DOC
	// according to configuration item
	var data = {};
	data.configurationitem = configurationitem_get();
	data.sourcetype = sourcetype_get();
	var async = true;
	artus_xhr("submittedfor", data, async, "GET");
}

var probability_values = {'VH': 0.9, 'H':0.6, 'M':0.4, 'L':0.25, 'VL':0.1};
var impact_values = {'VH':16, 'H':8.5, 'M':4, 'L':2, 'VL':0.5};

function _render_rating(rating_value) {
    var rating_attr = {'value': rating_value}
    if (rating_value < 0.85) {
        rating_attr['text'] = 'G';
        rating_attr['bgcolor'] = '#03fd00';
        rating_attr['color'] = 'black';
    }
    else if (rating_value >= 0.85 && rating_value < 3.6) {
        rating_attr['text'] = 'A';
        rating_attr['bgcolor'] = '#ff9a00';
        rating_attr['color'] = 'black';
    }
    else {
        rating_attr['text'] = 'R';
        rating_attr['bgcolor'] = '#ff0100';
        rating_attr['color'] = 'white';
    }
    return rating_attr;
}

function on_evaluation_change() {
	var probability_field = document.getElementById('field-probability');
	if (document.getElementById('field-probability')) {
		var probability_options = document.getElementById('field-probability').options;
		var probability = probability_options[document.getElementById('field-probability').selectedIndex].text;
		var impact_options = document.getElementById('field-impact').options;
		var impact = impact_options[document.getElementById('field-impact').selectedIndex].text;
		var rating_value = probability_values[probability]*impact_values[impact];
		var rating_attr = _render_rating(rating_value);
		var field_rating = document.getElementById('field-rating');
		field_rating.style.backgroundColor = rating_attr['bgcolor'];
		field_rating.style.color = rating_attr['color'];
		field_rating.value = rating_attr['text'];
		field_rating.title = 'Rating Value: ' + Math.floor(rating_attr['value']*100)/100;
		field_rating.className = 'tooltip';
	}
}

function on_ecmtype_change(ticket_type, program_name) {
	var ecmtype = ecmtype_get();

	if (ecmtype == 'Document Delivery') {
		$("label[for=field-fromecm]").hide();
		$("#field-fromecm").hide();
		versionsuffix_set("");
		on_versionsuffix_change(ticket_type, program_name);
	}
	else {
		$("label[for=field-fromecm]").show();
		$("#field-fromecm").show();
		fromecm_filter();
	}
}

function on_ecrtype_change() {
	var ecrtype;
	var field_ecrtype = document.getElementById('field-ecrtype');
	if (field_ecrtype) {
		ecrtype = field_ecrtype.options[field_ecrtype.selectedIndex].text;
	}
	else {
		ecrtype = $("td[headers=h_ecrtype]>a").text().trim();
	}
	// Ticket box update
	var h_blocking = document.getElementById('h_blocking');
    if (h_blocking != null) {
	    if (ecrtype == 'Evolution') {
	    	h_blocking.innerHTML="Parent ECR(s):";
        }
	    else {
	    	h_blocking.innerHTML="Parent EFR(s):";
	    }
    }
    // Ticket properties update
	var $field_blocking = $("input[name=field_blocking]");
	if ($field_blocking.length != 0) {
		var tooltip_instances = $.tooltipster.instances($field_blocking);
		if (tooltip_instances.length == 0) {
			$field_blocking.tooltipster({
				theme: 'tooltipster-noir',
			});
			$field_blocking.tooltipster({
				theme: 'tooltipster-noir',
				content: 'In case of customer ECM or DCR, use attachments instead',
				multiple: true,
				side: 'bottom',
				delay: 900
			});
		}
		tooltip_instances = $.tooltipster.instances($field_blocking);
		tooltip_instances[1].disable();
	}
	// fields accessible with current profile and workflow
	var $blocking = $("a#blocking");
	if ($blocking.length != 0) {
		if (ecrtype == 'Evolution') {
			$blocking.html("Parent ECR(s):");  // 'blocking' is an added anchor element
			tooltip_instances[0].content('TRAC ticket numbers. Ex: 2,5,7 Those tickets can only be closed after this ticket is closed');
			tooltip_instances[1].enable();
		}
		else {
			$blocking.html("Parent EFR(s):");  // 'blocking' is an added anchor element
			tooltip_instances[0].content('TRAC ticket numbers that are closed by this ticket. Ex: 1,4,6');
			tooltip_instances[1].disable();
		}
	}
}

function force_edit_mode(trac_env_name, id, filename) {
	// The last parameter is optional
	// It is used only for attachments
	var location;
	var confirm_message;
	if (filename != undefined) {
	  // attachment
	  location = '/tracs/' + trac_env_name + '/attachment/ticket/' + id + '/' + filename;
	}
	else {
	  // ticket
	  location = '/tracs/' + trac_env_name + '/ticket/' + id;
	}
    if (gup("merged") != "") {
        location += '?merged=True';
    }
    if ($('#force-edit-mode').prop('checked')) {
      confirm_message = "You should not edit the ";
      if (filename != undefined) {
    	// attachment
        confirm_message += "attachment";
      }
      else {
    	// ticket
        confirm_message += "ticket form";
      }
      confirm_message += " as you are not the ticket owner or the current workflow status is not appropriate. Check FIRST the ticket owner has submitted his/her own changes. If not a conflict will arise. Then answer 'OK'";
      jqConfirm(confirm_message, null, function(confirmed) {
    	  if(confirmed) {
    		  if (gup("merged") != "") {
    			  document.location = location + '&forced=True';
    		  }
    		  else {
    			  document.location = location + '?forced=True';
    		  }
    		  set_overlay();
    	  }
    	  else {
    		  $('#force-edit-mode').prop('checked', false);
    	  }
      });
    }
    else {
      document.location = location;
      set_overlay();
    }
}

function mom_lock_unlock(id) {
	var data = {};
	data.ticket_id = id;
	var async = false;
	if (document.getElementById('force-edit-mode').checked) {
		// Try and get Subversion lock
		data.action = "mom_lock";
    }
    else {
        // Release Subversion lock
		data.action = "mom_unlock";
    }
	set_overlay();
	artus_xhr("force-edit-mode", data, async, "POST");
	var location = '/tracs/' + g_trac_env_name + '/ticket/' + id;
	if (gup("merged") != "") {
		location += '?merged=True';
	}
	if (document.getElementById('force-edit-mode').checked) {
	    if (gup("merged") != "") {
	    	document.location = location + '&forced=True';
	    }
	    else {
	      document.location = location + '?forced=True';
	    }
	}
	else {
		document.location = location;
	}
}

function mom_regenerate(trac_env_name, id) {
	var confirm_message;
	confirm_message = "Please check you are not currently editing the ticket before regenerating it. Also your edits will be LOST ! So do CONFIRM before going on !";
	jqConfirm(confirm_message, null, function(confirmed) {
  	  if(confirmed) {
  		  // Pre-fill MOM
  		  var data = {};
  		  data.action = "regenerate";
  		  var async = true;
  		  artus_xhr("pre-fill_mom", data, async, "POST");
  		  set_overlay();
  	  }
	});
}

function on_sourcefile_change() {
	let data;
	let sourcefile = $("select[name=field_sourcefile]").val();
	let ticket_id = ticketid_get();
	let async = true;
	// Go and get sourcefile url for viewing source file before selecting
	data = {};
	data.sourcefile = sourcefile;
	data.ticket_id = ticket_id;
	artus_xhr("sourcefile_url", data, async, "GET");
	// Enable or disable Edit/View button
	// if sourcefile does or doesn't exist in the WC
	data = {};
	data.docfile = sourcefile;
	data.ticket_id = ticket_id;
	artus_xhr("sourcefile_exists", data, async, "GET");
}

function on_pdffile_change() {
	let data;
	let pdffile = $("select[name=field_pdffile]").val();
	let ticket_id = ticketid_get();
	let async = true;
	// Go and get pdffile url for viewing before selecting
	data = {};
	data.pdffile = pdffile;
	data.ticket_id = ticket_id;
	artus_xhr("pdffile_url", data, async, "GET");
	// Enable or disable View button
	// if pdffile does or doesn't exist in the WC
	data = {};
	data.docfile = pdffile;
	data.ticket_id = ticket_id;
	artus_xhr("pdffile_exists", data, async, "GET");
}

function get_src_locker() {
	var data = {};
	data.ticket_id = ticketid_get();
	var async = true;
	artus_xhr("src_locker", data, async, "GET");
}

function get_pdf_locker() {
	var data = {};
	data.ticket_id = ticketid_get();
	var async = true;
	artus_xhr("pdf_locker", data, async, "GET");
}

function check_external_lock_on_src() {
	var data = {};
	data.ticket_id = ticketid_get();
	var async = true;
	artus_xhr("src_repos_status", data, async, "GET");
	set_overlay();
}

function check_external_lock_on_pdf() {
	var data = {};
	data.ticket_id = ticketid_get();
	var async = true;
	artus_xhr("pdf_repos_status", data, async, "GET");
	set_overlay();
}

function schedule_lock() {
	// Schedule an automatic edition mode set
	var data = {};
	data.action = "schedule_lock";
	var async = true;
	artus_xhr("edit-doc-file", data, async, "POST");
}

function unschedule_lock() {
	// Unschedule an automatic edition mode set
	var data = {};
	data.action = "unschedule_lock";
	var async = true;
	artus_xhr("edit-doc-file", data, async, "POST");
}

function lock_files() {
	// Cleaning from an eventual previous Unlock operation
	$('iframe#clickOnce').remove();
	// WC update + lock
	var data = {};
	data.action = "lock";
	var async = true;
	artus_xhr("edit-doc-file", data, async, "POST");
}

function unlock_change() {
	if (typeof g_ticket_type !== "undefined") {
		if (g_ticket_type == 'DOC') {
			let source_file = $("select[name=field_sourcefile]").val();
			if (source_file.endsWith('.docm')) {
				// Inclusion of attachments into PDF
				let pdf_file = $("select[name=field_pdffile]").val();
				if (pdf_file == "N/A") {
					unlock_files();
				}
				else {
					// Dialog to choose whether to include attachments
					$( "#dialog-document-properties" ).dialog("open");
					if (g_doc_exist_attachments) {
						// Attachments included
						$("#attachments-included").prop("checked", true);
						$("#attachments-not-included").prop("checked", false);
					}
					else {
						// Attachments not included
						$("#attachments-included").prop("checked", false);
						$("#attachments-not-included").prop("checked", true);
					}
				}
			}
			else {
				unlock_files();
			}
		}
		else {
			unlock_files();
		}
	}
}

function beforeUnload(event) {
	let url = window.location.protocol + "//" + window.location.host + "/tracs/" + g_trac_env_name + "/beacon";
	if (typeof g_trac_id != 'undefined') {
		let searchParams = new URLSearchParams("ticket_id=" + g_trac_id);
		navigator.sendBeacon(url, searchParams);
	}
}

function set_beforeunload_event_handler() {
	// **** Setup of an unlocking mechanism for the semaphore on the server side. ****
	// In case things do not go as expected, the semaphore may not be released adequately.
	// The user will then not be able to display a page associated with a DOC/ECM/FEE ticket
	// and will probably try to escape this situation by navigating away from this page.
	// The unload event is sent to the window element when the user navigates away from the page.
	// This could mean one of many things. The user could have clicked on a link to leave the page,
	// or typed in a new URL in the address bar. The forward and back buttons will trigger the event.
	// Closing the browser window will cause the event to be triggered.
	// Even a page reload will first create an unload event.
	if (typeof g_doc_lock != 'undefined' && g_doc_lock == true) {
		window.addEventListener('beforeunload', beforeUnload);
		beforeunload_event_handler_set = true;
	}
}

function unset_beforeunload_event_handler () {
	if (beforeunload_event_handler_set == true) {
		window.removeEventListener("beforeunload", beforeUnload);
		beforeunload_event_handler_set = false;
	}
}

function launch_clickOnce()
{
	// launch PDF generation on user desktop
	let href = this.location.protocol + "//" + this.location.host + "/tracs/" + g_trac_env_name + "/clickonce";
	if (typeof(g_doc_sourcefile_status) != 'undefined') {
		// DOC
		href += '?ticket_id=' + g_trac_id + '&status=' + g_doc_sourcefile_status + '&attachments=' + g_doc_pdffile_attachments + '&charts=' + g_doc_sourcefile_charts + '&markups=' + g_doc_pdffile_markups + '&automation=' + g_doc_automation;
	}
	else {
		// ECM, FEE
		href += '?ticket_id=' + g_trac_id + '&charts=' + g_doc_sourcefile_charts + '&markups=' + g_doc_pdffile_markups + '&automation=' + g_doc_automation;
	}
	// The insertion of an iframe will launch clickOnce without generating an unload event for the document
	$('<iframe src="' + href + '" frameborder="0" scrolling="no" id="clickOnce"></iframe>').appendTo('#unlock');
}

function unlock_files() {
	set_overlay();
	// Wait until unlock is complete
	let data = {};
	data.action = "wait_unlock";
	let async = true;
	artus_xhr("edit-doc-file", data, async, "POST");
	// The wait will be cancelled by a page unload
	set_beforeunload_event_handler();
	// Launch the actions associated with Unlock
	launch_clickOnce();
}

function set_src_modified()
{
	if (!$("#src_wc_status").length) {
		$('select#field-sourcefile').after('<span id="src_wc_status">*</span>');
		$('span#src_wc_status').css('color', 'red');
	}
}

function unset_src_modified()
{
	$('#src_wc_status').remove();
}

function set_pdf_modified()
{
	if (!$("#pdf_wc_status").length) {
		$('select#field-pdffile').after('<span id="pdf_wc_status">*</span>');
		$('span#pdf_wc_status').css('color', 'red');
	}
}

function unset_pdf_modified()
{
	$('#pdf_wc_status').remove();
}

function set_focus(element)
{
	if (element == 'lock') {
		$('input#lock').parent().css('border', '1px solid red');
	}
	else if (element == 'unlock') {
		$('input#unlock').parent().css('border', '1px solid red');
	}
	else if (element == 'edit') {
		$('input#source_edit').parent().css('border', '1px solid red');
	}
}

function unset_focus(element)
{
	if (element == 'lock') {
		$('input#lock').parent().css('border', '0px solid red');
	}
	else if (element == 'unlock') {
		$('input#unlock').parent().css('border', '0px solid red');
	}
	else if (element == 'edit') {
		$('input#source_edit').parent().css('border', '0px solid red');
	}
}

function edit_view_file()
{
	if ($("input[name=lock_unlock]:checked").val() == 'lock') {
		location.href = g_doc_sourcefile_button_webdav_edit_url;
		/*  The opened document is automatically saved therefore modified */
		set_src_modified();
		unset_focus('lock');
		set_focus('unlock');
		unset_focus('edit');
	}
	else
	{
		location.href = g_doc_sourcefile_button_webdav_view_url;
	}
}

function on_field_change(fieldnames)
{
	var g_trac_data_modified = false;
	var elt, i;
	for (i = 0; i < fieldnames.length; i++) {
		elt = $("#field-" + fieldnames[i]);
		if (elt.val() != elt.prop("defaultValue")) {
			g_trac_data_modified = true;
		}
	}
	if (g_trac_data_modified) {
		setup_workflow("True");
		schedule_lock();
	}
	else {
		setup_workflow("False");
		unschedule_lock();
	}
}

function merging_done(trac_env_name, id) {
	if ($('#ticket_form-merging-done').length > 0 && $('#ticket_form-merging-done').prop('checked')) {
      let confirm_message = "You will no more have access to your ticket form changes but only to the merged ones. If you are OK with this, answer 'OK'";
      jqConfirm(confirm_message, null, function(confirmed) {
      	  if(confirmed) {
      		  if (gup("forced") != "") {
      			  document.location = '/tracs/' + trac_env_name + '/ticket/' + id + '?forced=True&ticket_form_merged=True';
      		  }
      		  else {
      			  document.location = '/tracs/' + trac_env_name + '/ticket/' + id + '?ticket_form_merged=True';
      		  }
      		  set_overlay();
      	  }
      	  else {
      		  $('#ticket_form-merging-done').prop('checked', false);
      	  }
      });
	}
    else if ($('#attachment-merging-done').length > 0 && $('#attachment-merging-done').prop('checked')) {
      let confirm_message = "You will no more have access to your attachments changes but only to the merged ones. If you are OK with this, answer 'OK'";
      jqConfirm(confirm_message, null, function(confirmed) {
      	  if(confirmed) {
      		  if (gup("forced") != "") {
      			  document.location = '/tracs/' + trac_env_name + '/ticket/' + id + '?forced=True&attachment_merged=True';
      		  }
      		  else {
      			  document.location = '/tracs/' + trac_env_name + '/ticket/' + id + '?attachment_merged=True';
      		  }
      		  set_overlay();
      	  }
      	  else {
      		$('#attachment-merging-done').prop('checked', false);
      	  }
      });
    }
    else {
        if (gup("forced") != "") {
            document.location = '/tracs/' + trac_env_name + '/ticket/' + id + '?forced=True';
        }
        else {
            document.location = '/tracs/' + trac_env_name + '/ticket/' + id;
        }
        set_overlay();
    }
}

function skill_is_unmanaged(program_name, name)
{
    var regex$ = "^"+program_name+"_("+g_unmanaged_skills+")_";
	var regex = new RegExp(regex$);
	var results = regex.exec(name);
	return results != null;
}

function gup(name)
{
  /* cf http://www.netlobo.com/url_query_string_javascript.html */
  name = name.replace(/[\[]/,"\\\[").replace(/[\]]/,"\\\]");
  var regex$ = "[\\?&]"+name+"=([^&#]*)";
  var regex = new RegExp(regex$);
  var results = regex.exec(document.location.href);
  if( results == null)
    return "";
  else
    return results[1];
}

function get_query_string(qsParm) {
	/*
	 * Receive an array of (key,val) pairs
	 * and return the associated query string
	 *
	 * qsParam: array to be processed
	 */
	 var qs = '?' + qsParm[0][0] + '=' + qsParm[0][1];
	 for (var i=1; i<qsParm.length; i++) {
		qs += '&' + qsParm[i][0] + '=' + qsParm[i][1];
	 }
	 return qs;
}

function remove_from_array(array_in, key) {
	/*
	 *   Remove all occurrences of a (key,value) pair from a two-dimensional array
	 *   passed in by reference
	 *
	 *   array_in: array to be processed
	 *   key: key of the (key,value) pair to be removed
	 */
	var i = array_in.length;
	while (i--){
		if (array_in[i][0] == key) {
			array_in.splice(i, 1);
		}
	}
}

function original_event(event, value, text) {
	if (typeof(event.explicitOriginalTarget) != 'undefined') {
		// click on the radio button itself
		if (typeof(event.explicitOriginalTarget['value']) != 'undefined') return (event.explicitOriginalTarget['value'] == value);
		// click on the label
		else return event.explicitOriginalTarget.textContent == text
	}
	else {
		return true;
	}
}

function get_href(keys_to_set, keys_to_unset) {
	/*
	 * Get the query string, remove keys_to_set keys
	 * and optionally keys_to_unset keys
	 * and setup the new query string with keys_to_set values
	 * and return the new href
	 */

	 var href = document.location.href;

	 /* return unmodified href in case of error */
	 if (typeOf(keys_to_set) != 'array' || typeOf(keys_to_unset) != 'array') {
		 return href
	 }

	 /* Unset all keys */
	 var qs_params = parse_query_string();
	 for (var i=0; i < keys_to_set.length; i++) {
		 remove_from_array(qs_params, keys_to_set[i][0]);
	 }
	 for (var i=0; i < keys_to_unset.length; i++) {
		 remove_from_array(qs_params, keys_to_unset[i]);
	 }

	 /* Construct new href with only untouched parameters */
	 if (href.indexOf('?') != -1) {
		 href = href.substring(0, href.indexOf('?'));
	 }
	 if (qs_params.length != 0) {
		 href += get_query_string(qs_params);
	 }

	 var sep = '';
	 if (qs_params.length == 0) {
		 sep = '?';
	 }
	 else {
		 sep = '&';
	 }

	 /* Set specified keys */
	 for (var i=0; i < keys_to_set.length; i++) {
		 var set_value = '' + keys_to_set[i][1];
		 href += sep + keys_to_set[i][0] + '=' + urlEncode(set_value.trim());
		 if (sep == '?') {
			 sep = '&';
		 }
	 }

	 set_overlay();
	 return href
}

function getBrowser()
{
	if ("ActiveXObject" in window)
	{
		// Internet Explorer
		return 'IE';
	}
	else if (/Edge/.test(navigator.userAgent)) {
		// Edge
		return 'Edge';
	}
	else
	{
		// Other
		return '';
	}
}

var setRevertHandler = function() {
	$("button.trac-revert").click(function() {
		var div = $("div", this);
		var field_name = div[0].id.substr(7);
		var field_value = div.text();
		var input = $("#propertyform *[name=field_" + field_name + "]");
		if (input.length > 0) {
			if (input.filter("input[type=radio]").length > 0) {
				input.val([field_value]);
			} else if (input.filter("input[type=checkbox]").length > 0) {
				input.val(field_value == "1" ? [field_value] : []);
			} else {
				input.val(field_value);
			}
		} else { // Special case for CC checkbox
			input = $("#propertyform input[name=cc_update]").val([]);
		}
		if (field_name == "sourceurl") {
			// Special case for "sourceurl": update associated Browse button
			/* Update onclick attribute */
			var ci_select = $("input[name='ci_select']");
			var onclick = ci_select.attr("onclick");
			var regex = /(location.href=".+\/browser)(.+)(&caller=t\d+")/;
			ci_select.attr("onclick", onclick.replace(regex, "$1" + field_value + "$3"));
			/* Update title attribute */
			ci_select.attr("title", field_value);
			/* Update Source File/PDF File lists */
			var data = {};
			data.sourceurl = field_value;
			var async = true;
			artus_xhr("sourcefile", data, async, "GET");
			artus_xhr("pdffile", data, async, "GET");
		}
		input.change();
		$(this).closest("li").remove();
		return false;
	});
}

function isHTML(str) {
	var doc = new DOMParser().parseFromString(str, "text/html");
	return Array.from(doc.body.childNodes).some(node => node.nodeType === 1);
}

// Modal alert function

function jqAlert(outputMsg, titleMsg, onCloseCallback) {
	if (!outputMsg)
		outputMsg = 'No Message to Display.';
    if (!titleMsg)
        titleMsg = 'Sorry';
    var isHtml = isHTML(outputMsg);
    var htmlContents, width, height, resizable;
    if (isHtml) {
    	htmlContents = outputMsg;
    	width = 1400;
    	height = 600;
    	resizable = true;
    }
    else {
    	htmlContents = '<p style="text-align:justify;-ms-hyphens:auto;-webkit-hyphens:auto;hyphens:auto">' +
		outputMsg + '</p>';
    	width = 300;
    	height = "auto";
    	resizable = false;
    }
    var html = $('<div id="jqAlert"></div>').html(htmlContents);
    html.dialog({
        title: titleMsg,
        width: width,
        height: height,
        resizable: resizable,
        modal: true,
        create: function() {
        	$(this).closest('div.ui-dialog')
            .find('.ui-dialog-titlebar-close').hide();
            if (!isHtml) {
        		$(this).dialog("option", "buttons",
                	[
        	        	{
        	        		text: "OK",
        	        		click: function() {
        	        			$(this).dialog("close");
        	        		}
        	        	}
                	]
        		);
            }
        },
        close: function() {
        	if (onCloseCallback) onCloseCallback();
        	/* Cleanup node(s) from DOM */
        	$(this).remove();
       	}
    });
}

// Modal confirm function

function jqConfirm(outputMsg, titleMsg, onCloseCallback) {
	if (!outputMsg)
		outputMsg = 'No Message to Display.';
    if (!titleMsg)
        titleMsg = 'Please confirm !';
    var jqConfirmed = false;
    var paragragh = '<p style="text-align:justify;-ms-hyphens:auto;-webkit-hyphens:auto;hyphens:auto">' +
    				outputMsg.replace(/\n/g, '</br>') + '</p>';
    var html = $('<div id="jqConfirm"></div>').html(paragragh);
    html.dialog({
        title: titleMsg,
        resizable: false,
        modal: true,
        create: function() {
        	$(this).closest('div.ui-dialog')
            .find('.ui-dialog-titlebar-close').hide();
        },
        buttons: {
            "OK": function () {
            	jqConfirmed = true;
                $(this).dialog("close");
            },
            "Cancel": function () {
            	jqConfirmed = false;
                $(this).dialog("close");
            }
        },
        close: function() {
        	if (onCloseCallback) onCloseCallback(jqConfirmed);
            /* Cleanup node(s) from DOM */
            $(this).remove();
        }
    });
}

// UI components
var UIComponents = {
	buttons: {
		UIButton : {
			constructor: function(buttonBuilder, message) {
				var constructor = function() {
					// Instantiate only when DOM is ready
					this.button = buttonBuilder();
					this.message = message;
					this.submit_previewed = false;
					this.submit_confirmed = false;
					var that = this;
					// Call-back functions
					this.clickIfTrue = function(confirmed) {
						if (confirmed) {
							that.submit_confirmed = true;
							that.click();
						}
					}
					this.postIfTrue1 = function(confirmed) {
						if (confirmed) {
							that.submit_confirmed = true;
							/*
							 * cf https://stackoverflow.com/questions/39716481/how-to-submit-multipart-formdata-using-jquery
							 * cf https://stackoverflow.com/questions/38277900/formdata-object-does-not-add-submit-type-inputs-from-form-while-on-firefox
							 */
							var data = new FormData(that.button.closest("form")[0]);
							data.append(that.button.attr('name'), that.button.val());
							$.ajax({
							        url: "",
							        method: "POST",
							        data: data,
							        processData: false,
							        contentType: false,
							        success: function(data, textStatus, jqXHR){
							        	document.write(jqXHR.responseText);
								    },
							        error: function(jqXHR, textStatus, errorThrown){
										document.write(jqXHR.responseText);
							        }
							});
							set_overlay();
						}
					}
					this.postIfTrue2 = function(confirmed) {
						if (confirmed) {
							that.submit_confirmed = true;
							var data = that.button.closest("form").serializeArray();
							let url = window.location.pathname;
							let final_url = url.substring(0, url.lastIndexOf("/"));
							$.post("", data, function(response, status, xhr) {
								window.location.href = final_url;
			                });
							set_overlay();
						}
					}
				};
				constructor.prototype.enable = function() {
					if (this.button && this.button.length > 0) this.button.prop("disabled", false);
				}
				constructor.prototype.disable = function() {
					if (this.button && this.button.length > 0) this.button.prop("disabled", true);
				}
				constructor.prototype.click = function() {
					if (this.button && this.button.length > 0) this.button.trigger("click");
				}
				constructor.prototype.confirm = function() {
				    if (!this.submit_confirmed) {
					    jqConfirm(this.message, null, this.clickIfTrue);
					    return false;
				    }
				    set_overlay();
				    return true;
				}
				return constructor;
			}
		},
		applyBranch: {
			constructor: function() {
				var constructor = new UIComponents.buttons.UIButton.constructor(function() {
					return $("input[value='Apply Branch']");},
					"A new branch will be created. Please confirm !");
				return constructor;
			}
		},
		addReplaceVersionTag: {
			constructor: function() {
				var constructor = new UIComponents.buttons.UIButton.constructor(function() {
					return $("form#add_tag input[value='Add/Replace']");});
				constructor.prototype.confirm = function(included_tag, replaced_tag) {
					if (included_tag != '' && replaced_tag != '') {
						if (!this.submit_confirmed) {
							let message = "OK to replace:\n" + replaced_tag + "\nwith:\n" + included_tag + " ?";
					    	jqConfirm(message, null, this.clickIfTrue);
					  		return false;
						}
				  	}
					set_overlay();
					return true;
				};
				return constructor;
			}
		},
		applyTag: {
			constructor: function() {
				var constructor = new UIComponents.buttons.UIButton.constructor(function() {
					return $("input[value='Apply Tag']");});
				constructor.prototype.confirm = function(status) {
					if (status == 'Proposed') {
						if (!this.submit_confirmed) {
							let message = "Do you confirm the status in your document header is 'Released' and the first page is signed ?";
					    	jqConfirm(message, null, this.clickIfTrue);
					  		return false;
						}
					}
					set_overlay();
					return true;
				};
				return constructor;
			}
		},
		addAttachment: {
			constructor: function() {
				var constructor = new UIComponents.buttons.UIButton.constructor(function() {
					return $("form#attachment input[name=add]");});
				constructor.prototype.confirm = function(status) {
				    if (!this.submit_confirmed) {
				    	let message = "Please check you are not currently editing the ticket (under LibreOffice or MS Office) before adding or removing attachments";
					    jqConfirm(message, null, this.postIfTrue1);
					    return false;
				    }
				};
				return constructor;
			}
		},
		deleteAttachment: {
			constructor: function() {
				var constructor = new UIComponents.buttons.UIButton.constructor(function() {
					return $("div#delete input[value='Delete attachment']");});
				constructor.prototype.confirm = function(status) {
				    if (!this.submit_confirmed) {
				    	let message = "Please check you are not currently editing the ticket (under LibreOffice or MS Office) before adding or removing attachments";
					    jqConfirm(message, null, this.postIfTrue2);
					    return false;
				    }
				};
				return constructor;
			}
		},
		CreateTicketSubmitChanges: {
			constructor: function() {
				var setup = function(elt) {
					/* Instantiate only when DOM is ready */
					const title = 'Send your changes to TRAC and Subversion';
					const tableHTML = '<table id="create_submit"><tr><td></td></tr></table>';
					const helpHTML = '<td style="vertical-align:middle"><a id="how_it_works" href="' + g_dc_url + '/index.php?post/67">How it works</a></td>';
					if (elt.length > 0) {
						// For tickets only
						elt.attr('onclick', 'return UIComponents.buttons.CreateTicketSubmitChanges.object.confirm()').attr('title', title).css('margin', '0 0.5em');
						elt.wrap(tableHTML);
						$('table#create_submit tr').append(helpHTML);
						return $("table[id=create_submit] input[name=submit]");
					}
				};
				var constructor = new UIComponents.buttons.UIButton.constructor(function() {
					return setup($('input[value="Create ticket"]').add('input[value="Submit changes"]'));});
				constructor.prototype.confirm = function() {
					var ticket_id = $("#field-summary").val();
					if (ticket_id == "") {
						jqAlert("The Ticket Identifier is empty ! The ticket cannot be created.");
						return false;
					}
					if ($("#indicate-merging-done").length > 0) {
						jqAlert("You have first to resolve the conflict as indicated at the top of the screen");
						return false;
					}
					if (g_ticket_type == 'MOM') {
						if (ticket_id.indexOf('YYYY-MM-DD') != -1) {
							jqAlert("You have not selected the scheduled date for the meeting");
							return false;
						}
						if ($("#field-momtype").length > 0) {
							var momtype = $("#field-momtype option:selected").text();
							if (momtype == 'CCB' || momtype == 'Review') {
								var milestonetag_options = $("#field-milestonetag option");
								if (milestonetag_options.length == 0) {
									jqAlert("You have not selected the milestone tag associated with the meeting");
									return false;
								}
							}
						}
					}
					if (ticket_id.endsWith("<Version Suffix>")) {
						jqAlert("You have to define the Version Suffix");
						return false;
					}
					if (g_ticket_type == 'DOC' && typeof g_ticket_status !== "undefined" && g_ticket_status == "01-assigned_for_edition") {
						if (!$("td[headers=h_sourcefile] > a").length && !$("td[headers=h_pdffile] > a").length) {
							if ($('#action_reassign').prop('checked')) {
								if ($("select#reassign_reassign_owner option:selected").text() == g_ticket_owner) {
									if (!this.submit_confirmed) {
										jqConfirm("If you assign the ticket to yourself, you will be added to the authors list. Please confirm you will be a document author.",
												null, this.clickIfTrue);
										return false;
									}
								}
							}
						}
					}
					if (g_ticket_type == 'ECR' && typeof g_ticket_status == "undefined") {
						// Shown only for non created ECR ticket
						if ($("#field-document").val() == '') {
							jqAlert("You have not selected the baseline tag");
							return false;
						}
					}
					if ($('#action_implement_ticket').prop('checked')) {
						if (g_ticket_type == 'ECR' || g_ticket_type == 'RF' || g_ticket_type == 'PRF') {
							if (!this.submit_confirmed) {
								let alert_msg;
								if (g_ticket_type == 'ECR') {
									alert_msg = "Have you committed the changes on the baseline tag BEFORE changing this ticket state to IMPLEMENTED?";
								}
								else if (g_p_rf_parent_ticket) {
									alert_msg = "Have you generated the PDF file of the reviewed document BEFORE changing this ticket state to IMPLEMENTED?";
								}
								else {
									alert_msg = "Have you committed the changes to the reviewed document BEFORE changing this ticket state to IMPLEMENTED?";
								}
								jqConfirm(alert_msg, null, this.clickIfTrue);
								return false;
							}
						}
					}
					if ($('#action_analyse_ticket').prop('checked')) {
						if(g_ticket_type == 'ECR') {
							if (!this.submit_confirmed) {
								jqConfirm("Have you listed the impacted requirements BEFORE changing the ticket state to ANALYSED?",
										null, this.clickIfTrue
								);
								return false;
							}
						}
					}
					set_overlay();
					if ($("input#field-blocking").length > 0 ||
							$("input#field-blockedby").length > 0) {
						if (!this.submit_previewed) {
							this.request();
							return false;
						}
					}
					return true;
				}
				constructor.prototype.request = function() {
					// Request a preview through XHR
				    // Construct request data
				    var form = $("#propertyform");
				    var that = this;
				    var data = form.serializeArray();
				    data.push({name: 'preview', value: '1'});

				    $.ajax({
				      type: form.attr('method'), url: form.attr('action'),
				      data: data, traditional: true, dataType: "html",
				      success: function(reply) {
				    	that.update(data, reply);
				    	that.submit_previewed = true;
				    	that.click();
				      },
				      error: function(jqXHR, textStatus, errorThrown) {
				        unset_overlay();
				      }
				    });
				}
				constructor.prototype.update = function(data, reply) {
				    var items = $(reply);
				    // Update view time
				    $("#propertyform input[name='view_time']").replaceWith(items.filter("input[name='view_time']"));
				    // Update preview
				    var preview = $("#ticketchange").html(items.filter('#preview').children());
				    var show_preview = preview.children().length != 0;
				    $("#ticketchange").toggle(show_preview);
				    setRevertHandler();
				    // Update masterticket fields
				    $("li.trac-field-blockedby.trac-conflict>button[name=revert_blockedby]").trigger("click");
				    $("li.trac-field-blocking.trac-conflict>button[name=revert_blocking]").trigger("click");
				}
				return constructor;
			}
		}
	}
};
