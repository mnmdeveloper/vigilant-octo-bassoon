param([string]$Manifest)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$items = Get-Content -LiteralPath $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    foreach ($item in $items) {
        $speaker.SelectVoice($item.voice)
        $speaker.Rate = $item.rate
        $speaker.SetOutputToWaveFile($item.path)
        $speaker.Speak($item.text)
        $speaker.SetOutputToNull()
    }
} finally { $speaker.Dispose() }
