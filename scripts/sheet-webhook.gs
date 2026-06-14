function doPost(e) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var d = JSON.parse(e.postData.contents);
  var ts = d.ts ? new Date(d.ts * 1000) : new Date();
  var ev = (d.event || '').toLowerCase();

  if (ev === 'search') {
    var s = ss.getSheetByName('Searches') || ss.insertSheet('Searches');
    if (s.getLastRow() === 0) s.appendRow(['Timestamp','Name','Email','Query','Results','Total pool','Relaxed']);
    s.appendRow([ts, d.name||'', d.email||'', d.query||'', d.results, d.total, d.relaxed||'']);
    return json({ok:true});
  }

  if (ev === 'save_search') {
    var sv = savedSheet(ss);
    var id = Utilities.getUuid();
    sv.appendRow([id, ts, (d.email||'').toLowerCase(), d.name||'', d.query||'', d.criteria||'']);
    return json({ok:true, id:id});
  }

  if (ev === 'delete_saved') {
    var sv = savedSheet(ss);
    var rows = sv.getDataRange().getValues();
    var email = (d.email||'').toLowerCase();
    for (var i = rows.length - 1; i >= 1; i--) {
      if (String(rows[i][0]) === String(d.id) && String(rows[i][2]).toLowerCase() === email) sv.deleteRow(i + 1);
    }
    return json({ok:true});
  }

  var g = ss.getSheetByName('Signups') || ss.getActiveSheet();
  g.appendRow([ts, d.event||'', d.name||'', d.email||'', d.user_agent||'']);
  return json({ok:true});
}

function doGet(e) {
  var p = (e && e.parameter) || {};
  if (p.action === 'list_saved') {
    var sv = savedSheet(SpreadsheetApp.getActiveSpreadsheet());
    var rows = sv.getDataRange().getValues();
    var email = (p.email||'').toLowerCase();
    var out = [];
    for (var i = 1; i < rows.length; i++) {
      if (String(rows[i][2]).toLowerCase() === email) {
        out.push({id:String(rows[i][0]), created_at:rows[i][1], email:rows[i][2], name:rows[i][3], query:rows[i][4], criteria:rows[i][5]});
      }
    }
    out.reverse();
    return json(out);
  }
  return json({ok:true});
}

function savedSheet(ss) {
  var sv = ss.getSheetByName('Saved Searches');
  if (!sv) sv = ss.insertSheet('Saved Searches');
  if (sv.getLastRow() === 0) sv.appendRow(['ID','Timestamp','Email','Name','Query','Criteria (JSON)']);
  return sv;
}

function json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
