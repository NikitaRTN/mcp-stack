Add-Type -AssemblyName System.Windows.Forms
$form = New-Object System.Windows.Forms.Form
$form.Text = 'MCP isolated UI test'
$form.Width=600; $form.Height=300; $form.StartPosition='CenterScreen'
$text=New-Object System.Windows.Forms.TextBox
$text.Name='mcpTestInput'; $text.AccessibleName='MCP test input'; $text.SetBounds(20,20,500,30)
$check=New-Object System.Windows.Forms.CheckBox
$check.Name='mcpTestCheck';$check.Text='MCP test checkbox';$check.SetBounds(20,65,250,30)
$button=New-Object System.Windows.Forms.Button
$button.Name='mcpTestButton';$button.Text='MCP test invoke';$button.SetBounds(20,110,250,40)
function Save-State {
    [ordered]@{text=$text.Text; checked=$check.Checked} | ConvertTo-Json -Compress | Set-Content -Encoding UTF8 'logs/uia-fixture-state.json'
}
$text.Add_TextChanged({Save-State})
$check.Add_CheckedChanged({Save-State})
$button.Add_Click({$text.Text='invoked'; Save-State})
Save-State
$form.Controls.AddRange(@($text,$check,$button))
$timer=New-Object System.Windows.Forms.Timer
$timer.Interval=180000;$timer.Add_Tick({$form.Close()});$timer.Start()
[System.Windows.Forms.Application]::Run($form)
