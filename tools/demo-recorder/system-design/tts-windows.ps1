# Text-to-speech for the system-design walkthrough (Windows only). Reads a JSON manifest [{ "text": "...", "file": "..." }]
# and writes one 24 kHz, 16-bit mono WAV file per entry with the System.Speech voices; existing files are kept (cache).
param(
    [Parameter(Mandatory = $true)] [string] $Manifest,
    [string] $Voice = "Microsoft Zira Desktop",
    [int] $Rate = 0
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech

$items = Get-Content -Raw -Encoding UTF8 -LiteralPath $Manifest | ConvertFrom-Json
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $synth.SelectVoice($Voice)
} catch {
    Write-Warning "Voice '$Voice' is not installed; using '$($synth.Voice.Name)'."
}
$synth.Rate = $Rate
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    24000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)

try {
    foreach ($item in @($items)) {
        if (Test-Path -LiteralPath $item.file) { continue }
        $part = "$($item.file).part"
        $synth.SetOutputToWaveFile($part, $format)
        $synth.Speak([string] $item.text)
        $synth.SetOutputToNull()
        Move-Item -LiteralPath $part -Destination $item.file -Force
    }
} finally {
    $synth.Dispose()
}
