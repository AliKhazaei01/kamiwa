# Kamiwa acceptance contract

## Approved decisions
- Complete test web app on an Ubuntu server. Test payment/SMS/email only until real services are configured; preview through SSH tunnel.
- Three paid 30-day default plans: PRO dealership, PLUS individual professional, BUYER. Identity reviewed by staff after mobile and email OTP verification. Experts and staff have internal roles.
- Username ASCII letters, case-insensitive. Password >=8 characters including lower/upper ASCII and digit. CAPTCHA and rate limiting for login.
- PRO page slug 1-8 ASCII letters; PLUS same base plus `_personal`; unique, case-insensitive, editable with redirect from old slug.
- Private identity: name, family name, national ID, birth date, mobile, email, province, city and identity document. PRO requires business name/address/license/activity. PLUS requires personal photo.
- Internal user code is unique random 10000-99999, visible only to authorized staff; database primary key is independent.
- Guest sees marketing and registration. Verified paid members can browse/search/contact/compare up to three/favorite. Province selection is profile default, user override, Tehran fallback.
- Main categories: truck, tractor, trailer and carrier. Active approved posts per main category: PRO 50, PLUS 25. Capacity is checked on submission and publication under transaction. BUYER cannot publish.
- Publication lifecycle and moderation are independent. Preserve approved version while edits await review. Proposed-price edits record history. Owner can mark sold/paused/disabled/archived/deleted. Expiry hides sellers' listings and preserves panel/renewal/tickets.
- PLUS sold history max 50 total; soft-delete earliest sold_at when adding item 51. PRO has no commercial sold-count cap. Seller profiles and promotional content are moderated.
- Dashboard: remaining days/capacity, listing/page views, favorites, price history. Contact channels are seller-selected phone/WhatsApp/Telegram/email.
- Each province has three valid experts maintained by staff. Each separately-paid request from buyer or seller is allocated to one expert by staff. Buyer requests notify seller for coordination.
- Expert and staff can submit reports with attribution; final approval by authorized staff. Final appraisal price and date always visible to entitled buyers. Sellers cannot alter or hide it.
- Negotiability (fixed/small discount/negotiable) is separate from computed market position. Similarity: same category/brand/model, year +/-1 and mileage +/-20%; active approved priced offers nationwide, excluding self. >=5 samples: Q25/Q75 range; otherwise insufficient data. Never call offer-price comparison a sale-price or appraisal result.
- Admin manages identity, ads, time-limited restrictions, disabling, granular staff permissions, subscription pricing, expertise fees, categories and two-way subject/message tickets. Sensitive actions audited without secrets.
- Main admin username and password rotate every 30 days; encrypted transient test outbox to the configured administrator email. Rotation idempotent/retry-safe, old sessions invalidated. Email changes require current-password reauthentication and verified new address, plus a fresh MFA code if enrolled.
- Private documents/reports outside public media; validate files/size/type, process images and strip metadata. Server-side permission checks, CSRF, session protection, CAPTCHA and throttling.

## Required verification
Meaningful domain and view tests for all roles, expiry, verification, payments (success/failure/delay/replay), moderation, quota boundaries/concurrency, sold retention, identifiers, appraisal immutability, private files, tickets, admin rotation and MFA. Browser tests at 360/768/1440, both themes, keyboard and real flows. Deployment, migration, backup/restore and modest resource/load tests. No open critical/high defects; lower issues explicitly recorded. No claim of production readiness until real integrations, HTTPS, independent backup and capacity assessment.

## User-directed amendment — 2026-09-15
- Remove compulsory MFA enrollment from staff login. Enrollment is optional for every role and begins only through an explicit CSRF-protected POST in profile settings. Opening the old setup URL returns to settings without showing or creating a secret. Already enrolled accounts still require their second factor, including direct Django-admin entry.
- Product manager approves this focused interaction change: a clearly labeled optional profile action, no enrollment gate before the panel, and an explicit explanation of future login requirements before opt-in confirmation. Preserve CAPTCHA, throttling, session revocation and object permissions.
- The user requested shareable trial access and selected the server IP instead of a domain. Prepare trusted HTTPS on the server's public IP, while preserving test-service labels. Public readiness must be verified from outside the server; firewall or certificate preparation alone is not acceptance.

## User-directed amendment — 2026-10-20
- Sign-up: live username/password feedback, show-password toggle, Jalali birth date (1300–today), numeric-only national ID and mobile; national card and birth certificate images are required and identity approval checks both.
- Approved accounts' identity/contact edits wait for staff approval; old values stay in force until then, and approval replaces data, deletes superseded documents and requires re-verification of a changed email/mobile.
- Staff: search by five-digit code opens a per-user page (identity, documents, all listings, activity, payments by permission); online/last-seen and daily activity minutes; the main administrator assigns section admins with one or more sections; successful sign-in queues an email notice.
- Inspections: staff see requests by province and may cancel, start a re-inspection, or edit/delete a report, including a final one. This replaces the earlier rule that a final appraisal is immutable for everyone; sellers still cannot change or hide it, and every staff change is audited with a report snapshot.
- Listings take up to 5 photos; Pro listings also take 2 videos (MP4/MOV, ≤ 1 minute, ≤ 1080p, ≤ 60 MB).
- Experts have a wallet: staff credit it (once per final inspection), experts request withdrawals to a validated IBAN; the amount leaves the balance immediately as "in progress" until staff mark it paid (or reject and refund). Test mode moves no money.
- Decorative images rotate from a pool of licensed CC0/public-domain photos; uploaded listing photos are never replaced.
