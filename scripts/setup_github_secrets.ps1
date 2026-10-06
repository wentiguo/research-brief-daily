param(
  [string]$Repo,
  [string]$GhPath = "",
  [string]$SmtpHost,
  [string]$SmtpPort = "587",
  [string]$SmtpUsername,
  [string]$SmtpFromEmail,
  [string]$SmtpToEmail,
  [string]$ContactEmail,
  [string]$SmtpUseSsl = "false",
  [string]$SmtpStartTls = "true",
  [switch]$ConfigureDeepSeek,
  [string]$DeepSeekModel = "deepseek-v4-pro",
  [string]$DeepSeekBaseUrl = "https://api.deepseek.com"
)

$ErrorActionPreference = "Stop"

# Resolve the GitHub CLI: explicit -GhPath, else PATH, else the Windows default install.
if ([string]::IsNullOrWhiteSpace($GhPath)) {
  $onPath = Get-Command gh -ErrorAction SilentlyContinue
  if ($onPath) { $GhPath = $onPath.Source }
  elseif (Test-Path "C:\Program Files\GitHub CLI\gh.exe") { $GhPath = "C:\Program Files\GitHub CLI\gh.exe" }
  else { throw "GitHub CLI (gh) not found. Install it, or pass -GhPath <path to gh.exe>." }
}


function Ask-IfMissing([string]$Value, [string]$Prompt) {
  if ([string]::IsNullOrWhiteSpace($Value)) {
    return Read-Host $Prompt
  }
  return $Value
}

function Set-SecretBody([string]$Name, [string]$Value) {
  & $GhPath secret set $Name --repo $Repo --body $Value
}

if (-not (Test-Path -LiteralPath $GhPath)) {
  throw "gh.exe not found at $GhPath"
}

$Repo = Ask-IfMissing $Repo "GitHub repository (owner/name)"
$SmtpHost = Ask-IfMissing $SmtpHost "SMTP host, e.g. smtp.gmail.com or smtp.qq.com"
$SmtpPort = Ask-IfMissing $SmtpPort "SMTP port, e.g. 587 or 465"
$SmtpUsername = Ask-IfMissing $SmtpUsername "SMTP username / login email"
$SmtpFromEmail = Ask-IfMissing $SmtpFromEmail "From email"
$SmtpToEmail = Ask-IfMissing $SmtpToEmail "To email"
$ContactEmail = Ask-IfMissing $ContactEmail "Contact email for scholarly API User-Agent"
$SmtpUseSsl = Ask-IfMissing $SmtpUseSsl "SMTP_USE_SSL true/false"
$SmtpStartTls = Ask-IfMissing $SmtpStartTls "SMTP_STARTTLS true/false"

& $GhPath auth status --hostname github.com | Out-Host

Set-SecretBody "SMTP_HOST" $SmtpHost
Set-SecretBody "SMTP_PORT" $SmtpPort
Set-SecretBody "SMTP_USERNAME" $SmtpUsername
Set-SecretBody "SMTP_FROM_EMAIL" $SmtpFromEmail
Set-SecretBody "SMTP_TO_EMAIL" $SmtpToEmail
Set-SecretBody "SMTP_USE_SSL" $SmtpUseSsl
Set-SecretBody "SMTP_STARTTLS" $SmtpStartTls
Set-SecretBody "SMTP_RETRIES" "3"
Set-SecretBody "RESEARCH_BRIEF_CONTACT_EMAIL" $ContactEmail

$securePassword = Read-Host "SMTP password / app authorization code" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
try {
  $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
  $plainPassword | & $GhPath secret set SMTP_PASSWORD --repo $Repo
} finally {
  if ($bstr -ne [IntPtr]::Zero) {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  }
  if ($plainPassword) {
    $plainPassword = $null
  }
}

Write-Host "SMTP GitHub Actions secrets configured for $Repo"

if ($ConfigureDeepSeek) {
  Set-SecretBody "DEEPSEEK_MODEL" $DeepSeekModel
  Set-SecretBody "DEEPSEEK_BASE_URL" $DeepSeekBaseUrl

  $secureDeepSeekKey = Read-Host "DeepSeek API key" -AsSecureString
  $deepSeekBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureDeepSeekKey)
  try {
    $plainDeepSeekKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($deepSeekBstr)
    $plainDeepSeekKey | & $GhPath secret set DEEPSEEK_API_KEY --repo $Repo
  } finally {
    if ($deepSeekBstr -ne [IntPtr]::Zero) {
      [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($deepSeekBstr)
    }
    if ($plainDeepSeekKey) {
      $plainDeepSeekKey = $null
    }
  }

  Write-Host "DeepSeek GitHub Actions secrets configured for $Repo"
}
