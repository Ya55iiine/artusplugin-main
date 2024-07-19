$(function () {
	$('#base').jstree({
		"core" : { // core options go here
			"multiple" : false, // no multiselection
			"themes" : {
				"dots" : false, // no connecting dots between dots
				"icons" : false
			}
		},
		"plugins" : ["state"] // activate the state plugin on this instance
	}).bind('ready.jstree', function(event, data) {
		var $tree = $(this);
		$($tree.jstree().get_json($tree, {
			flat: true
		})).each(function () {
			// Get the level of the node
			var node = $("#base").jstree().get_node(this.id);
			var level = node.parents.length;
			if (level <= 2) {
				$("#base").jstree().open_node({"id": node.id});
			}
	});
	}).bind("click", function(event) {
		if (event.target.tagName == 'A') {
			$('#base').jstree('save_state');
			parent.location.href = event.target.href;
		}
	}).bind("after_open.jstree", function (event, data) {
		var iframe = parent.document.getElementById('TracFeatures');
		if (iframe != null) {
			parent.resizeIframe(iframe); 
		}			
	}).bind("after_close.jstree", function (event, data) {
		var iframe = parent.document.getElementById('TracFeatures');
		if (iframe != null) {
			parent.resizeIframe(iframe); 
		}			
	});
});