# Windows Automation MCP — standard-library companion for MCP Hub.
$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class NativeInput {
 [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
 [DllImport("user32.dll")] public static extern void mouse_event(uint flags,uint dx,uint dy,uint data,UIntPtr extra);
}
'@
function Send-Json($value) {
    [Console]::Out.WriteLine(($value | ConvertTo-Json -Compress -Depth 30))
    [Console]::Out.Flush()
}
function Send-Result($id, $result) {
    Send-Json ([ordered]@{ jsonrpc='2.0'; id=$id; result=$result })
}
function Send-Error($id, $message, $code=-32000) {
    Send-Json ([ordered]@{ jsonrpc='2.0'; id=$id; error=[ordered]@{ code=$code; message=[string]$message } })
}
function Text-Result($value) {
    $json = $value | ConvertTo-Json -Compress -Depth 20
    return [ordered]@{ content=@([ordered]@{ type='text'; text=$json }); structuredContent=$value }
}function Schema($properties, $required=@()) {
    $schema = [ordered]@{ type='object'; properties=$properties; additionalProperties=$false }
    if ($required.Count) { $schema.required = $required }
    return $schema
}
function Tool($name, $description, $schema) {
    return [ordered]@{ name=$name; description=$description; inputSchema=$schema }
}
function Get-Tools {
    $selector = [ordered]@{
        processId=@{type='integer'; description='Target process ID'}
        name=@{type='string'; description='Exact accessible name'}
        controlType=@{type='string'; description='Window, Button, Edit, DataItem, RadioButton, CheckBox, etc.'}
        className=@{type='string'; description='Optional native/UIA class name'}
        occurrence=@{type='integer'; minimum=0; default=0}
    }
    return @(
        (Tool 'list_windows' 'List visible top-level Windows UI Automation windows.' (Schema ([ordered]@{ title=@{type='string'}; processId=@{type='integer'} })))
        (Tool 'list_controls' 'List accessible controls for one process, with names, types, bounds and supported patterns.' (Schema ([ordered]@{ processId=@{type='integer'}; name=@{type='string'}; controlType=@{type='string'}; className=@{type='string'}; limit=@{type='integer'; minimum=1; maximum=1000; default=250} }) @('processId')))
        (Tool 'invoke_control' 'Invoke a button, menu item or other accessible control without screen coordinates.' (Schema $selector @('processId')))
        (Tool 'set_control_value' 'Set the ValuePattern text of an accessible edit control.' (Schema ([ordered]@{} + $selector + [ordered]@{ value=@{type='string'} }) @('processId','value')))
        (Tool 'select_control' 'Select a list/radio item or set a checkbox toggle state.' (Schema ([ordered]@{} + $selector + [ordered]@{ state=@{type='boolean'; default=$true} }) @('processId')))
        (Tool 'click_control' 'Click the center of an accessible control after resolving it semantically.' (Schema $selector @('processId')))
        (Tool 'send_keys' 'Activate a process/window and send Windows SendKeys text.' (Schema ([ordered]@{ processId=@{type='integer'}; title=@{type='string'}; keys=@{type='string'} }) @('keys')))
        (Tool 'wait_for_control' 'Wait until a matching accessible control appears and optionally becomes enabled.' (Schema ([ordered]@{} + $selector + [ordered]@{ timeoutSeconds=@{type='number'; minimum=0.1; maximum=120; default=15}; enabled=@{type='boolean'} }) @('processId')))
        (Tool 'capture_screenshot' 'Capture the virtual desktop or a process window as PNG.' (Schema ([ordered]@{ processId=@{type='integer'}; title=@{type='string'}; desktop=@{type='boolean'; default=$false} })))
    )
}function Type-Name($element) {
    return ($element.Current.ControlType.ProgrammaticName -replace '^ControlType\.','')
}
function Element-Data($element) {
    $patterns = @($element.GetSupportedPatterns() | ForEach-Object { $_.ProgrammaticName -replace 'PatternIdentifiers.Pattern$','' })
    $rect = $element.Current.BoundingRectangle
    return [ordered]@{
        name=$element.Current.Name; controlType=(Type-Name $element)
        automationId=$element.Current.AutomationId; className=$element.Current.ClassName
        processId=$element.Current.ProcessId; enabled=$element.Current.IsEnabled
        offscreen=$element.Current.IsOffscreen
        bounds=[ordered]@{ left=[math]::Round($rect.Left); top=[math]::Round($rect.Top); width=[math]::Round($rect.Width); height=[math]::Round($rect.Height) }
        patterns=$patterns
    }
}
function Matches($element, $inputArgs) {
    if ($inputArgs.name -and $element.Current.Name -ne [string]$inputArgs.name) { return $false }
    if ($inputArgs.controlType -and (Type-Name $element) -ne [string]$inputArgs.controlType) { return $false }
    if ($inputArgs.className -and $element.Current.ClassName -ne [string]$inputArgs.className) { return $false }
    return $true
}
function Process-Elements($processId) {
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty, [int]$processId)
    return [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants, $condition)
}function Find-Control($inputArgs) {
    $items = Process-Elements ([int]$inputArgs.processId)
    $matches = @()
    for ($i=0; $i -lt $items.Count; $i++) {
        $element = $items.Item($i)
        if (Matches $element $inputArgs) { $matches += $element }
    }
    $index = if ($null -ne $inputArgs.occurrence) { [int]$inputArgs.occurrence } else { 0 }
    if ($index -lt 0 -or $index -ge $matches.Count) {
        throw "Control not found (matches=$($matches.Count), occurrence=$index)"
    }
    return $matches[$index]
}
function List-Windows($inputArgs) {
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $items = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
        [System.Windows.Automation.Condition]::TrueCondition)
    $result = @()
    for ($i=0; $i -lt $items.Count; $i++) {
        $element = $items.Item($i)
        if ($element.Current.IsOffscreen -or -not $element.Current.Name) { continue }
        if ($inputArgs.processId -and $element.Current.ProcessId -ne [int]$inputArgs.processId) { continue }
        if ($inputArgs.title -and $element.Current.Name -notlike "*$($inputArgs.title)*") { continue }
        $result += Element-Data $element
    }
    return $result
}
function List-Controls($inputArgs) {
    $limit = if ($inputArgs.limit) { [math]::Min(1000,[math]::Max(1,[int]$inputArgs.limit)) } else { 250 }
    $items = Process-Elements ([int]$inputArgs.processId); $result = @()
    for ($i=0; $i -lt $items.Count -and $result.Count -lt $limit; $i++) {
        $element = $items.Item($i)
        if (Matches $element $inputArgs) { $result += Element-Data $element }
    }
    return $result
}function Invoke-Control($inputArgs) {
    $element = Find-Control $inputArgs
    $pattern = [System.Windows.Automation.InvokePattern]$element.GetCurrentPattern(
        [System.Windows.Automation.InvokePattern]::Pattern)
    $pattern.Invoke(); return Element-Data $element
}
function Set-ControlValue($inputArgs) {
    $element = Find-Control $inputArgs
    $pattern = [System.Windows.Automation.ValuePattern]$element.GetCurrentPattern(
        [System.Windows.Automation.ValuePattern]::Pattern)
    $pattern.SetValue([string]$inputArgs.value); return Element-Data $element
}
function Select-Control($inputArgs) {
    $element = Find-Control $inputArgs
    if ($element.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern,[ref]$pattern)) {
        ([System.Windows.Automation.SelectionItemPattern]$pattern).Select()
    } elseif ($element.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern,[ref]$pattern)) {
        $toggle = [System.Windows.Automation.TogglePattern]$pattern
        $wanted = if ($null -eq $inputArgs.state) { $true } else { [bool]$inputArgs.state }
        $isOn = $toggle.Current.ToggleState -eq [System.Windows.Automation.ToggleState]::On
        if ($isOn -ne $wanted) { $toggle.Toggle() }
    } else { throw 'Control supports neither SelectionItemPattern nor TogglePattern' }
    return Element-Data $element
}
function Click-Control($inputArgs) {
    $element = Find-Control $inputArgs; $rect = $element.Current.BoundingRectangle
    if ($rect.Width -le 0 -or $rect.Height -le 0) { throw 'Control has no clickable bounds' }
    [NativeInput]::SetCursorPos([int]($rect.Left+$rect.Width/2),[int]($rect.Top+$rect.Height/2)) | Out-Null
    [NativeInput]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
    [NativeInput]::mouse_event(4,0,0,0,[UIntPtr]::Zero)
    return Element-Data $element
}
function Send-Keys($inputArgs) {
    $shell = New-Object -ComObject WScript.Shell
    $activated = if ($inputArgs.processId) { $shell.AppActivate([int]$inputArgs.processId) } else { $shell.AppActivate([string]$inputArgs.title) }
    if (-not $activated) { throw 'Target window could not be activated' }
    Start-Sleep -Milliseconds 150; $shell.SendKeys([string]$inputArgs.keys)
    return [ordered]@{ activated=$true; sent=[string]$inputArgs.keys }
}function Wait-Control($inputArgs) {
    $timeout = if ($inputArgs.timeoutSeconds) { [math]::Min(120,[double]$inputArgs.timeoutSeconds) } else { 15 }
    $deadline = [DateTime]::UtcNow.AddSeconds($timeout); $last = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $last = Find-Control $inputArgs
            if ($null -eq $inputArgs.enabled -or $last.Current.IsEnabled -eq [bool]$inputArgs.enabled) {
                return Element-Data $last
            }
        } catch { }
        Start-Sleep -Milliseconds 200
    }
    throw "Timed out after $timeout seconds waiting for control"
}
function Screenshot($inputArgs) {
    if ($inputArgs.desktop) {
        $screen = [System.Windows.Forms.SystemInformation]::VirtualScreen
        $rect = New-Object System.Drawing.Rectangle($screen.Left,$screen.Top,$screen.Width,$screen.Height)
    } else {
        $windows = List-Windows $inputArgs
        if (-not $windows.Count) { throw 'Target window not found' }
        $w = $windows[0].bounds
        $rect = New-Object System.Drawing.Rectangle($w.left,$w.top,$w.width,$w.height)
    }
    if ($rect.Width -le 0 -or $rect.Height -le 0) { throw 'Screenshot bounds are empty' }
    $bitmap = New-Object System.Drawing.Bitmap($rect.Width,$rect.Height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.CopyFromScreen($rect.Left,$rect.Top,0,0,$rect.Size)
        $stream = New-Object System.IO.MemoryStream
        $bitmap.Save($stream,[System.Drawing.Imaging.ImageFormat]::Png)
        $bytes = $stream.ToArray()
    } finally { $graphics.Dispose(); $bitmap.Dispose(); if ($stream) { $stream.Dispose() } }
    return [ordered]@{ content=@(
        [ordered]@{ type='image'; data=[Convert]::ToBase64String($bytes); mimeType='image/png' },
        [ordered]@{ type='text'; text="Captured $($rect.Width)x$($rect.Height) PNG" }
    ); structuredContent=[ordered]@{ width=$rect.Width; height=$rect.Height; bytes=$bytes.Length } }
}function Call-Tool($name, $inputArgs) {
    switch ($name) {
        'list_windows' { return Text-Result (List-Windows $inputArgs) }
        'list_controls' { return Text-Result (List-Controls $inputArgs) }
        'invoke_control' { return Text-Result (Invoke-Control $inputArgs) }
        'set_control_value' { return Text-Result (Set-ControlValue $inputArgs) }
        'select_control' { return Text-Result (Select-Control $inputArgs) }
        'click_control' { return Text-Result (Click-Control $inputArgs) }
        'send_keys' { return Text-Result (Send-Keys $inputArgs) }
        'wait_for_control' { return Text-Result (Wait-Control $inputArgs) }
        'capture_screenshot' { return Screenshot $inputArgs }
        default { throw "Unknown tool: $name" }
    }
}
while ($null -ne ($line = [Console]::In.ReadLine())) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    $request = $null
    try {
        $request = $line | ConvertFrom-Json
        $method = [string]$request.method
        if ($method -eq 'initialize') {
            Send-Result $request.id ([ordered]@{
                protocolVersion='2025-03-26'; capabilities=[ordered]@{ tools=[ordered]@{} }
                serverInfo=[ordered]@{ name='windows-automation-mcp'; version='1.0.0' }
            })
        } elseif ($method -eq 'tools/list') {
            Send-Result $request.id ([ordered]@{ tools=@(Get-Tools) })
        } elseif ($method -eq 'tools/call') {
            $params = $request.params
            Send-Result $request.id (Call-Tool ([string]$params.name) ($params.arguments))
        } elseif ($method -eq 'ping') {
            Send-Result $request.id ([ordered]@{})
        } elseif ($null -ne $request.id) {
            Send-Error $request.id "Method not found: $method" -32601
        }
    } catch {
        if ($request -and $null -ne $request.id) { Send-Error $request.id $_.Exception.Message }
    }
}