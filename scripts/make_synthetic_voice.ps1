# Local Windows TTS fixture only. No microphone or external TTS provider.
$ErrorActionPreference = 'Stop'
$cashlyBackendRoot = Split-Path -Parent $PSScriptRoot
$cashlyFixtureDirectory = Join-Path $cashlyBackendRoot '.pytest-tmp-vn-live-assets'
New-Item -ItemType Directory -Force -Path $cashlyFixtureDirectory | Out-Null
Add-Type -AssemblyName System.Speech
$cashlyVoiceSynth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $cashlyVoiceSynth.SelectVoice('Microsoft Zira Desktop')
    $cashlyVoiceSynth.Rate = -1
    $cashlyVoiceSynth.SetOutputToWaveFile((Join-Path $cashlyFixtureDirectory 'expense.wav'))
    $cashlyVoiceSynth.Speak('Hari ini saya beli nasi padang dua belas ribu, bayar QRIS.')
} finally {
    $cashlyVoiceSynth.Dispose()
}
Write-Output 'Synthetic WAV fixture ready; no user media recorded.'
