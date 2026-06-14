function doPost(e) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var d = JSON.parse(e.postData.contents);
  var ts = d.ts ? new Date(d.ts * 1000) : new Date();

  if ((d.event || '').toLowerCase() === 'search') {
    var s = ss.getSheetByName('Searches') || ss.insertSheet('Searches');
    if (s.getLastRow() === 0) {
      s.appendRow(['Timestamp', 'Name', 'Email', 'Query', 'Results', 'Total pool', 'Relaxed']);
    }
    s.appendRow([ts, d.name || '', d.email || '', d.query || '', d.results, d.total, d.relaxed || '']);
  } else {
    var g = ss.getSheetByName('Signups') || ss.getActiveSheet();
    g.appendRow([ts, d.event || '', d.name || '', d.email || '', d.user_agent || '']);
  }

  return ContentService.createTextOutput(JSON.stringify({ ok: true }))
    .setMimeType(ContentService.MimeType.JSON);
}
