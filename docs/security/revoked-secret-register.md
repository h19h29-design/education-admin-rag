# Revoked secret register

This register intentionally records metadata only. It must never contain the credential value, a reconstructable fragment, or a provider-console export.

## Historical browser credential finding

| Field | Value |
| --- | --- |
| Provider | Google Generative Language API |
| Scanner fingerprint | `5cde1d5caec84c0f0d71d3ceb1b97db05b60550b:교육행정_AI_Launcher.html:generic-api-key:364` |
| Rule | `generic-api-key` |
| Path | `교육행정_AI_Launcher.html` |
| First repository exposure commit | `5cde1d5caec84c0f0d71d3ceb1b97db05b60550b` (2026-07-27T11:08:24+09:00) |
| Last repository exposure commit | `5cde1d5caec84c0f0d71d3ceb1b97db05b60550b` (2026-07-27T11:08:24+09:00) |
| History evidence | Full reachable Git history scan at implementation time reported this one matching finding. |
| Provider revocation UTC | PENDING — provider-console evidence required. |
| Usage / billing anomaly review | PENDING — provider-console evidence required. |
| Approver | PENDING — provider-console evidence required. |

Repository remediation removed the browser credential and direct model-call path. This is not proof of external credential revocation. Provider revocation and usage review remain deployment blockers.
