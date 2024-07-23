/*
 Copyright (C) 2021 Cinc
 All rights reserved.

 This software is licensed as described in the file COPYING, which
 you should have received as part of this distribution.
*/
jQuery(document).ready(function($) {
    function apply_transform(filter_list){
        var i;
        for(i = 0; i < filter_list.length; i++){
          var html = filter_list[i];
          switch(html['pos']){
            case 'after':
              $(html['css']).after(html['html']);
              break;
            case 'append':
              $(html['css']).append(html['html']);
              break;
            case 'before':
              $(html['css']).before(html['html']);
              break;
            case 'prepend':
              $(html['css']).prepend(html['html']);
              break;
            case 'remove':
              $(html['css']).remove();
              break;
            case 'replace':
              $(html['css']).replaceWith(html['html']);
              break;
            default:
              break;
          };
        } // for
    };
  
    if(typeof childtkt_filter !== 'undefined'){
        apply_transform(childtkt_filter);
    };
  
    function apply_show_desc(){
      if ($('#ct-show-desc').is(':checked'))
        $(".ct-desc-row").show();
      else
        $(".ct-desc-row").hide();
    };
  
    function apply_show_header(){
      if ($('#ct-show-header').is(':checked'))
        $(".ct-tbl-header").show();
      else
        $(".ct-tbl-header").hide();
    };
  
    // Helper for saving preferences in user's session.
    // Taken from Tracs ticket page, form_token is added by Trac.
    var savePrefs = function(key, value) {
      var data = {
        save_prefs: true,
        __FORM_TOKEN: form_token
      };
      data[key] = value;
      $.ajax({ url: $('#ct-prefs').attr('action'), type: 'POST', data: data, dataType: 'text' });
    };
  
    // Checkbox is already properly set
    apply_show_desc();
    apply_show_header();
  
    $('#ct-show-desc').on('change', function() {
       apply_show_desc();
       savePrefs('ct_show_desc', $('#ct-show-desc').is(':checked'));
    });
    $('#ct-show-header').on('change', function(){
      apply_show_header();
      savePrefs('ct_show_header', $('#ct-show-header').is(':checked'));
    })
  });