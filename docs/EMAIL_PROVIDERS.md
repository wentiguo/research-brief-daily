# Email provider examples

This project can send email through either generic SMTP or SendGrid. SMTP is usually enough.

## Common SMTP settings

| Provider | SMTP_HOST | SMTP_PORT | SMTP_USE_SSL | SMTP_STARTTLS | Notes |
| --- | --- | ---: | --- | --- | --- |
| Gmail | smtp.gmail.com | 587 | false | true | Usually requires 2FA + App Password |
| Outlook / Microsoft 365 | smtp.office365.com | 587 | false | true | Some tenants block SMTP AUTH |
| QQ Mail | smtp.qq.com | 465 | true | false | Requires SMTP service + authorization code |
| 163 Mail | smtp.163.com | 465 | true | false | Requires SMTP service + authorization code |
| Custom university/company mail | ask your IT docs | 465 or 587 | depends | depends | Use the hostname that matches the TLS certificate |

## Required GitHub Secrets for SMTP

```text
SMTP_HOST
SMTP_PORT
SMTP_USERNAME
SMTP_PASSWORD
SMTP_FROM_EMAIL
SMTP_TO_EMAIL
SMTP_USE_SSL
SMTP_STARTTLS
```

Optional:

```text
SMTP_RETRIES
```

## Required GitHub Secrets for SendGrid

```text
SENDGRID_API_KEY
RESEARCH_BRIEF_FROM_EMAIL
RESEARCH_BRIEF_TO_EMAIL
```

## Testing safely

Use GitHub Actions manual run with `send_email=false` first. After the workflow succeeds, run it again with `send_email=true`.
