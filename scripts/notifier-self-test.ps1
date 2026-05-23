# scripts/notifier-self-test.ps1 - Windows analogue of notifier-self-test.sh
#
# Confirms that the Schwab MCP notifier paths work on Windows 10/11:
#   1. Toast notification via Windows.UI.Notifications (no third-party module).
#   2. Marker file at ~/Desktop/SCHWAB_REAUTH_NEEDED.md (the fallback channel).
#
# This is the Tier A best-effort equivalent of scripts/notifier-self-test.sh.

$ErrorActionPreference = "Stop"
$Title = "Schwab MCP"
$Msg   = "Schwab MCP self-test - if you see this, notifications work."

# 1) Toast via Windows.UI.Notifications (built-in, no extra module required).
try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
    $tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1)
    $tpl.GetElementsByTagName("text")[0].AppendChild($tpl.CreateTextNode($Title)) | Out-Null
    $tpl.GetElementsByTagName("text")[1].AppendChild($tpl.CreateTextNode($Msg))   | Out-Null
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($Title).Show(
        [Windows.UI.Notifications.ToastNotification]::new($tpl)
    )
    Write-Host "[ok] toast fired (check Action Center)"
} catch {
    Write-Warning "[skip] Windows.UI.Notifications unavailable: $($_.Exception.Message)"
    Write-Warning "       Install plyer (pip install schwab-marketdata-mcp[windows]) for an alternative."
}

# 2) Fallback marker file on the Desktop.
$Desktop = [Environment]::GetFolderPath("Desktop")
if (Test-Path $Desktop) {
    $MarkerBody = @"
# Schwab MCP - self-test marker

This file was created by ``scripts/notifier-self-test.ps1`` to confirm the
fallback markdown channel is working. You can safely delete it.
"@
    $MarkerPath = Join-Path $Desktop "SCHWAB_REAUTH_NEEDED.md"
    Set-Content -Path $MarkerPath -Value $MarkerBody -Encoding UTF8
    Write-Host "[ok] wrote $MarkerPath"
} else {
    Write-Warning "[skip] Desktop folder not found at $Desktop"
}
