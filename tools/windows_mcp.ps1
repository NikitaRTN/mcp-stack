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
function Send-Error($id, $message, [int]$code=-32000) {
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
        windowName=@{type='string'; description='Exact top-level window title; use to avoid scanning unrelated windows'}
        name=@{type='string'; description='Exact accessible name'}
        controlType=@{type='string'; description='Window, Button, Edit, DataItem, RadioButton, CheckBox, etc.'}
        automationId=@{type='string'; description='Exact stable UI Automation ID'}
        className=@{type='string'; description='Optional native/UIA class name'}
        occurrence=@{type='integer'; minimum=0; default=0}
    }
    return @(
        (Tool 'list_windows' 'List visible top-level Windows UI Automation windows.' (Schema ([ordered]@{ title=@{type='string'}; processId=@{type='integer'} })))
        (Tool 'list_controls' 'List accessible controls for one process, with names, types, bounds and supported patterns.' (Schema ([ordered]@{ processId=@{type='integer'}; windowName=@{type='string'}; name=@{type='string'}; automationId=@{type='string'}; controlType=@{type='string'}; className=@{type='string'}; limit=@{type='integer'; minimum=1; maximum=1000; default=250} }) @('processId')))
        (Tool 'get_control_details' 'Read control value and selection/toggle state; password fields are redacted.' (Schema $selector @('processId')))
        (Tool 'invoke_control' 'Invoke a button, menu item or other accessible control without screen coordinates.' (Schema $selector @('processId')))
        (Tool 'set_control_value' 'Set the ValuePattern text of an accessible edit control.' (Schema ([ordered]@{} + $selector + [ordered]@{ value=@{type='string'} }) @('processId','value')))
        (Tool 'select_control' 'Select a list/radio item or set a checkbox toggle state.' (Schema ([ordered]@{} + $selector + [ordered]@{ state=@{type='boolean'; default=$true} }) @('processId')))
        (Tool 'click_control' 'Click the center of an accessible control after resolving it semantically.' (Schema $selector @('processId')))
        (Tool 'send_keys' 'Activate a process/window and send Windows SendKeys text.' (Schema ([ordered]@{ processId=@{type='integer'}; title=@{type='string'}; keys=@{type='string'} }) @('keys')))
        (Tool 'wait_for_control' 'Wait until a matching accessible control appears and optionally becomes enabled.' (Schema ([ordered]@{} + $selector + [ordered]@{ timeoutSeconds=@{type='number'; minimum=0.1; maximum=120; default=15}; enabled=@{type='boolean'} }) @('processId')))
        (Tool 'capture_screenshot' 'Capture a compact preview or save locally without base64 output. Defaults to JPEG at max 1280px width.' (Schema ([ordered]@{ processId=@{type='integer'}; title=@{type='string'}; desktop=@{type='boolean'; default=$false}; maxWidth=@{type='integer'; minimum=320; maximum=3840; default=1280}; format=@{type='string'; enum=@('jpeg','png'); default='jpeg'}; saveTo=@{type='string'; description='Optional absolute local file path'}; includeImage=@{type='boolean'; default=$true} })))
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
    if ($inputArgs.automationId -and $element.Current.AutomationId -ne [string]$inputArgs.automationId) { return $false }
    return $true
}
function Process-Elements($processId, $windowName) {
    if ([int]$processId -le 0) { throw 'A positive processId is required' }
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty, [int]$processId)
    # Only traverse target windows, never every desktop control.
    $windows = [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
        [System.Windows.Automation.TreeScope]::Children, $condition)
    $result = New-Object 'System.Collections.Generic.List[System.Windows.Automation.AutomationElement]'
    foreach ($window in $windows) {
        if ($windowName -and $window.Current.Name -ne [string]$windowName) { continue }
        $result.Add($window)
        $children = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
        foreach ($child in $children) { $result.Add($child) }
    }
    return ,$result
}
function Find-Control($inputArgs) {
    $items = Process-Elements ([int]$inputArgs.processId) $inputArgs.windowName
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
    $items = Process-Elements ([int]$inputArgs.processId) $inputArgs.windowName; $result = @()
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
    $pattern = $null
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
        if (-not $inputArgs.processId -and -not $inputArgs.title) { throw 'Specify processId, title, or desktop=true' }
        $windows = @(List-Windows $inputArgs)
        if (-not $windows.Count) { throw 'Target window not found' }
        $w = $windows[0].bounds
        $rect = New-Object System.Drawing.Rectangle($w.left,$w.top,$w.width,$w.height)
    }
    if ($rect.Width -le 0 -or $rect.Height -le 0) { throw 'Screenshot bounds are empty' }
    $maxWidth = if ($inputArgs.maxWidth) { [math]::Max(320,[math]::Min(3840,[int]$inputArgs.maxWidth)) } else { 1280 }
    $format = if ($inputArgs.format) { [string]$inputArgs.format } else { 'jpeg' }
    if ($format -notin @('jpeg','png')) { throw 'format must be jpeg or png' }
    $bitmap = New-Object System.Drawing.Bitmap($rect.Width,$rect.Height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $small = $null; $scaledGraphics = $null; $stream = $null
    try {
        $graphics.CopyFromScreen($rect.Left,$rect.Top,0,0,$rect.Size)
        $image = $bitmap
        if ($rect.Width -gt $maxWidth) {
            $height = [math]::Max(1,[int]($rect.Height*$maxWidth/$rect.Width))
            $small = New-Object System.Drawing.Bitmap($maxWidth,$height)
            $scaledGraphics = [System.Drawing.Graphics]::FromImage($small)
            $scaledGraphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $scaledGraphics.DrawImage($bitmap,0,0,$maxWidth,$height); $image = $small
        }
        $width = $image.Width; $height = $image.Height
        $stream = New-Object System.IO.MemoryStream
        $imageFormat = if ($format -eq 'png') { [System.Drawing.Imaging.ImageFormat]::Png } else { [System.Drawing.Imaging.ImageFormat]::Jpeg }
        $image.Save($stream,$imageFormat); $bytes = $stream.ToArray()
        $saved = $null
        if ($inputArgs.saveTo) {
            if (-not [System.IO.Path]::IsPathRooted([string]$inputArgs.saveTo)) { throw 'saveTo must be an absolute path' }
            $saved = [System.IO.Path]::GetFullPath([string]$inputArgs.saveTo)
            if ([System.IO.File]::Exists($saved)) { throw 'saveTo already exists; choose a new path' }
            [System.IO.File]::WriteAllBytes($saved,$bytes)
        }
    } finally {
        if ($scaledGraphics) { $scaledGraphics.Dispose() }; if ($small) { $small.Dispose() }
        $graphics.Dispose(); $bitmap.Dispose(); if ($stream) { $stream.Dispose() }
    }
    $items = @([ordered]@{type='text'; text="Captured $($width)x$($height) $format ($($bytes.Length) bytes)"})
    if ($null -eq $inputArgs.includeImage -or $inputArgs.includeImage) {
        $items += [ordered]@{type='image'; data=[Convert]::ToBase64String($bytes); mimeType="image/$format"}
    }
    return [ordered]@{content=$items; structuredContent=[ordered]@{
        width=$width; height=$height; originalWidth=$rect.Width; originalHeight=$rect.Height
        bytes=$bytes.Length; format=$format; path=$saved
    }}
}
function Control-Details($inputArgs) {
    $element = Find-Control $inputArgs
    $data = Element-Data $element
    $data.isPassword = $element.Current.IsPassword
    $pattern = $null
    if (-not $data.isPassword -and $element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern,[ref]$pattern)) {
        $data.value = ([System.Windows.Automation.ValuePattern]$pattern).Current.Value
        $data.readOnly = ([System.Windows.Automation.ValuePattern]$pattern).Current.IsReadOnly
    }
    $pattern = $null
    if ($element.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern,[ref]$pattern)) {
        $data.toggleState = [string]([System.Windows.Automation.TogglePattern]$pattern).Current.ToggleState
    }
    $pattern = $null
    if ($element.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern,[ref]$pattern)) {
        $data.selected = ([System.Windows.Automation.SelectionItemPattern]$pattern).Current.IsSelected
    }
    return $data
}
function Call-Tool($name, $inputArgs) {
    switch ($name) {
        'list_windows' { return Text-Result ([ordered]@{windows=@(List-Windows $inputArgs)}) }
        'list_controls' { return Text-Result ([ordered]@{controls=@(List-Controls $inputArgs)}) }
        'get_control_details' { return Text-Result (Control-Details $inputArgs) }
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
                serverInfo=[ordered]@{ name='windows-automation-mcp'; version='1.2.0' }
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