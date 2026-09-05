# Windows UI Automation 1.2.0 — scoped desktop workflow

## Prefer narrow queries
1. list_windows identifies the target process and exact top-level window title.
2. Pass processId + windowName to list_controls and all semantic control tools. windowName is exact, not a substring. A missing window returns no controls, never falls back to the whole process.
3. Add controlType or a specific name/automationId. On Workshop Manager use DataItem, Button, Edit, RadioButton as separate queries. Listing every type can still hang in a third-party Qt provider even with window scoping. Client timeouts do not forcibly cancel such provider calls.
4. get_control_details reads ValuePattern value/readOnly, toggleState, and selected when supported. Password values are not returned. This is opt-in, not a dump of every field.
5. Qt table cells may advertise Invoke without selecting their row. Use click_control on the exact DataItem, then inspect whether the intended action button is enabled.
6. After setting text, read it back. QTextEdit can expose leading whitespace; compare trimmed text only where surrounding whitespace is semantically irrelevant. Do not blindly resubmit after ambiguous errors.

## Workshop update verification
Select the existing submission, then Re-Upload (not New Submission). Verify the intended addon, unchanged title, and requested visibility. Do not accept contest/legal checkboxes automatically. Submit once, then inspect Steam workshop_log.txt for the exact existing item and a successful upload result. Reopen the metadata editor to verify saved visibility; cancel that verification dialog without saving.

## Deployment and tests
The PowerShell source is loaded when a new stateful MCP session starts. Version 1.2.0 and ten tools were confirmed through the public authenticated connection; existing sessions were not interrupted. Refresh/reconnect the Windows integration if its existing session still exposes nine tools. Hub, proxy, and Desktop service do not need restarting.

Run the opt-in desktop fixture suite in an interactive Windows session:

```powershell
$env:MCP_TEST_LIVE_UI='1'
python -m unittest discover -s tests -v
```

Recorded result: 76 tests passed, including real field write/read, checkbox state, and a non-existent window returning an empty list. Evidence: logs/uia12-tests.err and logs/uia-scope-test.json. No credentials are included in these results.

Before deploying, back up the changed source files outside Git. Never publish config backups containing credentials.
