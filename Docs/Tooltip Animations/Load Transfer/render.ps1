# Renders the Load Transfer tooltip animation.
#   render.ps1 -Keys        -> only a few key frames (key_*.png) to check the look
#   render.ps1              -> every frame + tooltip.mp4 (ffmpeg from ShareX)
param([switch]$Keys)
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Add-Type -AssemblyName System.Drawing
Add-Type -Path (Join-Path $dir 'LoadTransferAnim.cs') -ReferencedAssemblies System.Drawing
$icon = "C:\Users\Bheemeshwer.Dodda\Documents\Ele\ElePyRevitExtension\EleTools.extension\Electrical.tab\Electrical.panel\3 More.stack\TransferLoads.pushbutton\icon.png"

if ($Keys) {
    $field = [LoadTransferAnim].GetField('icon', [Reflection.BindingFlags]'NonPublic,Static')
    $field.SetValue($null, [System.Drawing.Image]::FromFile($icon))
    foreach ($s in 1.0, 3.95, 6.9, 9.8, 12.4) {
        $bmp = New-Object System.Drawing.Bitmap 720, 405
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        [LoadTransferAnim]::Frame($g, $s)
        $bmp.Save((Join-Path $dir ("key_{0}.png" -f $s)))
        $g.Dispose(); $bmp.Dispose()
    }
    Get-ChildItem $dir -Filter key_*.png | ForEach-Object { $_.Name }
    return
}

$frames = Join-Path $dir 'frames'
if (Test-Path $frames) { Remove-Item $frames -Recurse -Force }
$n = [LoadTransferAnim]::Render($frames, $icon)
Write-Host "$n frames"
$ffmpeg = "C:\Users\Bheemeshwer.Dodda\AppData\Local\Programs\ShareX\ffmpeg.exe"
$out = Join-Path $dir 'tooltip.mp4'
& $ffmpeg -y -loglevel error -framerate 30 -i (Join-Path $frames 'f%04d.png') -vf "pad=720:406:0:0:0x0F172A" -c:v libx264 -preset slow -crf 22 -pix_fmt yuv420p -movflags +faststart $out
Get-Item $out | ForEach-Object { "{0}  {1:N0} bytes" -f $_.Name, $_.Length }
