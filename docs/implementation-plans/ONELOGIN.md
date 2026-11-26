# ONELOGIN: OneLogin's sign-in rules in Time & Fees' own authentication

Status: **Plan ready for review**. No code has changed.

## 1. Original requirement

"We have to add the same logic as the OneLogin, not the code." Time & Fees' own sign-in must
follow the Capium OneLogin approach (`C:\Users\vansh\Downloads\OneLogin\ONELOGIN_LOGIN_IMPLEMENTATION_PROMPT.md`:
account rules, lockout, two-factor sign-in with the MFA challenge rules, refresh rotation, activity
record), implemented in Time & Fees. Nothing connects to the OneLogin service, and no OneLogin
code is copied.

## 2. Interpretation

* Keep Time & Fees' authentication (its endpoints, envelope, error codes, sessions, tokens) and
  add the OneLogin rules it does not have yet.
* "Same logic" means the same **behaviour**: lockout after repeated failures with a 423, a
  `requires two factor` step answered by a six-digit TOTP code, challenges that expire, count
  attempts and work once, TOTP set-up and confirmation, and every sign-in event recorded.
* The OneLogin contract's paths and camelCase bodies are **not** copied: Time & Fees keeps its
  own conventions (`/api/v1/auth/...`, snake_case, `{success, data, ...}` envelope,
  `UPPER_SNAKE` codes, OpenAPI descriptions naming them).

## 3. Existing implementation, and what OneLogin's logic adds

| OneLogin rule | Time & Fees today | Change |
|---|---|---|
| Refresh rotated; reuse revokes the family | Done: `app/repositories/sessions.py` `rotate`, reuse revokes the session (`revoked_reason = 'reuse_detected'`), audited `auth.refresh_reuse_detected` | None |
| `LastLoginDate` on success | Done: `users.last_login_at` | None |
| Refuse deleted / disabled accounts | Done: `deleted_at`, `is_active` (`AUTH_ACCOUNT_DISABLED`) | None |
| Activity for login, failed login, logout | Done: audit `auth.login`, `auth.login_failed`, `auth.logout`, `auth.logout_all` | Add the MFA and lockout events |
| `me`, change password, logout revokes | Done: `GET /users/me`, `POST /users/me/password`, `/auth/logout` | None |
| **Lock after N consecutive failures, 423** | Missing: only rate limits (`login:ip` 10 and `login:email` 5 per 15 minutes, answering 429); nothing records consecutive failures on the account | **Add** |
| **Two-factor sign-in** (`requiresTwoFactor`, then validate a code) | Missing | **Add** |
| **MFA challenge rules** (expiry, `AttemptCount`/`MaxAttempts`, `IsUsed`, `IsRevoked`) | Missing | **Add** |
| **TOTP set-up and confirmation** | Missing | **Add** (plus turning it off, and an administrator reset for a lost phone) |
| Licence end date per user | Not per user; a practice whose subscription is not active is refused (`ensure_subscription_active`) | Decision D3 |

Relevant code: `app/services/auth.py` (`login`, `refresh`, `logout`), `app/api/v1/auth.py`,
`app/schemas/auth.py`, `app/db/models/user.py`, `app/repositories/users.py`,
`app/repositories/sessions.py`, `app/services/audit.py`, `app/services/rate_limit.py`,
`app/api/v1/users.py` (`/users/me`), the staff routes for administrators; UI
`src/features/auth/` (`LoginForm.tsx`, `AuthProvider.tsx`, `api/authApi.ts`, `schemas/`),
`src/features/account/` (the signed-in person's settings), `src/services/api/` (unchanged).

## 4. Root cause

Not a defect: Time & Fees' authentication predates the OneLogin rules and lacks lockout and
two-factor sign-in.

## 5. Proposed solution

### 5.1 Account lockout

* `users.failed_login_count` (int, default 0) and `users.locked_until` (UTC, null).
* A wrong password for an existing account adds 1; on the 5th consecutive failure the account is
  locked for 15 minutes (`auth.account_locked` audit row). A locked account answers **423
  `AUTH_ACCOUNT_LOCKED`** ("Too many failed sign-ins. Try again in N minutes or reset your
  password."), even with the right password, until `locked_until` passes.
* A successful sign-in resets the count. A password reset and an administrator's unlock clear
  both fields. An unknown email changes nothing and answers the same 401 as a wrong password, so
  the lockout never tells anyone which emails exist.
* The existing rate limits stay (they protect against spraying many accounts from one address).

### 5.2 Two-factor sign-in (TOTP, RFC 6238)

* `users.mfa_secret` (encrypted at rest with `MFA_ENCRYPTION_KEY`, Fernet), `users.mfa_enabled_at`.
* New table `mfa_challenges`, OneLogin's `MfaToken` rules: `id`, `user_id`, `token_hash` (the
  challenge handed to the browser, stored hashed), `expires_at` (5 minutes), `attempt_count`,
  `max_attempts` (5), `used_at`, `revoked_at`, `ip_address`, `user_agent`, `created_at`.
* `POST /auth/login` for an account with two-factor on and a right password: **no session yet**;
  answers `{requires_two_factor: true, challenge_token}`. With `totp_code` in the same request it
  completes at once.
* `POST /auth/2fa/validate` `{challenge_token, code}`: a right code ends the challenge (used) and
  starts the session exactly as a password sign-in does (same response and refresh cookie). A
  wrong code adds an attempt; at `max_attempts` the challenge is revoked (`AUTH_MFA_TOO_MANY_ATTEMPTS`).
  An expired, used or revoked challenge answers `AUTH_MFA_CHALLENGE_INVALID`. Wrong codes count
  toward the account lockout of 5.1 too.
* A code is accepted for the current 30-second step and one step either side; the same code can
  never be used twice for one account (last accepted step stored).
* Set-up for the signed-in person: `POST /users/me/2fa/setup` → `{secret, otpauth_uri}`
  (pending, not active yet); `POST /users/me/2fa/verify` `{code}` → active; `POST
  /users/me/2fa/disable` `{password, code}` → off. Changing two-factor ends the person's other
  sessions, as a password change does.
* Administrator reset (lost phone): `POST /organizations/{id}/staff/{user_id}/2fa/reset`, gated
  by the existing staff-management permission, audited (D4).
* Library: `pyotp` (pinned) for the TOTP arithmetic; `cryptography` (already installed) for Fernet.

### 5.3 Activity record (audit)

New account-level actions, written like today's sign-in rows (actor, IP, user agent, request
id): `auth.mfa_challenge_started`, `auth.mfa_failed`, `auth.mfa_login`, `auth.account_locked`,
`auth.account_unlocked`, `auth.mfa_enabled`, `auth.mfa_disabled`, `auth.mfa_reset`. The audit
catalogue (`app/services/audit_catalogue.py`) gets their labels.

### 5.4 UI

* `LoginForm.tsx`: when the answer is `requires_two_factor`, a second step asks for the six-digit
  code (numeric input, autofocus, paste-friendly), then calls `/auth/2fa/validate` and continues
  through the existing `startSession`. Messages for 401, 423 (with the minutes left), too many
  code attempts, expired challenge (back to the password step), network errors.
* Account settings (`src/features/account/`): a "Two-factor sign-in" card: set up (QR code of the
  `otpauth_uri` plus the secret for manual entry), confirm with a code, turn off with password and
  code. QR rendering: a small pinned dependency (`qrcode`), or the secret text only (D5).
* Staff detail page: "Reset two-factor" for administrators with the permission.
* Nothing changes in `src/services/api/` (token store, refresh, retry, tabs): the second step ends
  in the same session as today.

### 5.5 Database

One migration adding the four `users` columns and the `mfa_challenges` table (number by D1),
with a working downgrade; models and `alembic check` in step.

## 6. Decisions needed

* **D1. Which branch.** `main` runs PostgreSQL (the live site); `azure-mysql-migration` (MySQL)
  is unmerged. Recommended: **branch `onelogin-rules` from `azure-mysql-migration`** (migration
  `101`): it can be tested here, where MySQL runs. From `main` it ships sooner on PostgreSQL, but
  needs a local PostgreSQL to test, and the MySQL branch then regenerates its baseline to include it.
* **D2. Who must use two-factor.** Recommended: **optional per person** now. A practice setting
  "everyone must use two-factor" can follow as its own change.
* **D3. Per-user licence end date.** Recommended: **not added**; the practice subscription check
  already refuses access to a practice that cannot be used. Add it only if a person must lose
  access while their practice keeps it.
* **D4. Lockout and recovery values.** Recommended: lock after **5** consecutive failures, for
  **15 minutes**; unlocked by time, by a password reset, or by an administrator; administrators can
  reset a member's two-factor. Say if you want different numbers or no administrator reset.
* **D5. QR code.** Recommended: show a QR code (new UI dependency `qrcode`, about 30 KB) and the
  secret text. Alternative: the secret text only, no new dependency.

## 7. Files expected to change

| Area | Files |
|---|---|
| API | `app/db/models/user.py` (+ `MfaChallenge`), new migration, `app/core/config.py` (`MFA_ENCRYPTION_KEY`, lockout settings), new `app/core/totp.py`, `app/services/auth.py`, new `app/services/mfa.py`, `app/repositories/users.py`, new `app/repositories/mfa.py`, `app/schemas/auth.py`, `app/api/v1/auth.py`, `app/api/v1/users.py`, the staff router/service for the reset, `app/services/audit_catalogue.py`, `app/core/handlers.py` (423 for `AUTH_ACCOUNT_LOCKED` if needed), `requirements.txt` (`pyotp`), `.env.example`, `README.md` |
| API tests | new `tests/test_auth_lockout.py`, `tests/test_auth_two_factor.py`; existing auth tests unchanged |
| UI | `src/features/auth/components/LoginForm.tsx` (+ a code step component), `src/features/auth/api/authApi.ts`, `src/features/auth/schemas/`, `src/types/models.ts`, `src/features/account/` (two-factor card), the staff detail page, tests next to each |
| Docs | `docs/modules/foundation.md` (sign-in rules), `docs/api-reference.md`, `docs/modules/permissions.md` if a permission is added, `docs/domain.md` (account rules) |

## 8. Edge cases

Two tabs signing in at once (each gets its own challenge); a challenge reused after success
(refused); clock drift on the phone (one step either side); the account locked during the code
step (the code step answers 423 too); the account disabled or the password changed while a
challenge is open (refused); a person with two-factor whose secret is lost (administrator reset);
`MFA_ENCRYPTION_KEY` missing in production (startup refuses, like a short JWT key); a code
submitted twice quickly (single-use step); email case (`Ada@X` and `ada@x` are one account, as today).

## 9. Regression risks

| Risk | Mitigation |
|---|---|
| Lockout used to lock someone else out | Short lock (15 minutes), cleared by password reset; the same 401 for unknown emails; audit rows show who was locked and from where |
| Sign-in for people without two-factor changes | Their path is untouched: existing auth tests must pass unchanged |
| Two-factor bypass | No session is created before the code; the challenge is bound to the account, single use, short-lived, attempt-limited |
| Secret leakage | Encrypted at rest; never returned after set-up; not in logs or audit metadata |
| UI session machinery | Unchanged; the code step ends in the existing `startSession` |

## 10. Testing strategy

* API: lockout (count, 423, reset on success, cleared by reset and unlock, unknown email
  unaffected, audit rows); two-factor (set-up, verify, login without and with code, validate
  right/wrong/expired/used/revoked, attempts to the maximum, replayed code, disable, admin reset,
  sessions ended on change, audit rows); OpenAPI descriptions name the new codes; then the auth
  modules and the full suite.
* UI: the code step (MSW), messages per error, the set-up card; type check, lint, unit tests, build.
* By hand, locally: sign in with and without two-factor using an authenticator app, lock an
  account, reset it.

## 11. Acceptance criteria

* Valid sign-in without two-factor works as today; with two-factor it asks for a code and only
  then starts a session.
* 5 wrong passwords lock the account (423) for 15 minutes; a right password then still answers 423.
* A wrong code counts an attempt; an expired, used or revoked challenge is refused; a code cannot
  be replayed.
* Refresh rotation and reuse revocation behave as today.
* Every sign-in, failure, lockout and MFA event is in the audit log.
* All existing tests pass unchanged; lint, format, types, builds pass.
