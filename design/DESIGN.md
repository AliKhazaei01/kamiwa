# کامیوا — رابط کاربری مصوب

## Approval and reference

Product manager approved the desktop concept on 2026-09-11: navy/amber, pale gray canvas, white surfaces, right filter rail, image-led listing cards, and comparison tray. Correct the generated concept's duplicate minimum-price label; counts must come from real queries or clearly identified seeded test data. Framework: server-rendered Django templates + HTMX + vanilla CSS/JS.

- Desktop reference: `design/concept-marketplace.png`
- Coordinated mobile/day/night reference: `design/concept-mobile-dark.png`
- Generated via built-in Image Gen, ui-mockup use case. Exact brief: Persian RTL heavy-truck marketplace, authenticated full listing screen with right filters, three-column results, location and category selection, six truck images, prices, favorites, comparison tray; separate responsive daytime/nighttime continuation.

## Locked visual system

| Token | Day | Night |
|---|---|---|
| Page | #f5f7fa | #101c29 |
| Surface | #ffffff | #182a3a |
| Text/navy | #102c40 | #ecf2f7 |
| Secondary | #637381 | #a4b4c4 |
| Line | #dfe5ec | #304456 |
| Accent | #eeb344 | #eeb344 |
| Success | #21845b | #65c698 |
| Error | #b63434 | #fa9494 |

Vazirmatn variable font, locally served WOFF2; body 15/1.9, controls 14/1.6, small 12/1.7, page heading 32/1.5 (mobile 24), subheading 22/1.6, listing title 17/1.6. Prices use tabular numbers. Spacing scale 4/8/12/16/24/32/48/64px. Container max 1280px, desktop 32px and mobile 16px gutters. Corners 12px controls, 14px cards; restrained one-pixel borders; shadows only header/popovers/compare tray. No gradients or image tints. Product images retain natural light and color in both themes.

Icons are simple 24px currentColor line SVGs, 1.8px stroke, rounded caps/joins. Focus ring 3px amber with 3px offset; controls minimum 44px touch target. Motion 160ms, disabled under reduced-motion preference.

Desktop marketplace is two regions: 248–264px filters at right, flexible results with three columns. At 1100px two listing columns; below 760px filter rail becomes a disclosure, results one column, navigation condenses, and comparison remains reachable. Dashboard uses a side navigation and tables that horizontally scroll only inside their own region.

## Page and state inventory

| Surface | Required regions / actions |
|---|---|
| Public landing | Brand/nav, concise value proposition, daylight truck hero, join/sign in, account comparison, expertise process, FAQ, footer |
| Register | Account role, identity/contact/address/business/photo/document fields, username/password guidance, validation |
| Login | Username/password, captcha, submit, register link, expired-captcha/error state |
| Verification | Email/mobile status and OTP forms, identity review state, next step |
| Marketplace | Province/category/search/brand/price/inspection filters, sort, listing cards, pagination, comparison tray |
| Listing detail | Large media, title/specifications, seller price and mandatory finalized expert price/date, seller/contact actions, comparison/favorite, expertise request |
| Compare | Up to three selected vehicles, aligned specifications/prices/expertise; empty state |
| Seller page | Approved seller identity and location, photo, approved introduction, contact routes, active/sold listings and filters |
| Seller dashboard | Subscription days, per-category capacity, view/favorite analytics, listing management/status, links to create/expertise/tickets |
| Listing form | Grouped bound fields, uploads, moderation notice, save/cancel, errors |
| Plans | Buyer/plus/pro descriptions, capacity/duration/price, purchase/renew |
| Checkout | Explicit test payment state, summary, success/failure/delay simulation controls |
| Inspection list/detail | Requester/seller/expert role-appropriate requests, stage, payment, expert/report action, finalized price/date |
| Request/report forms | Role-appropriate form fields, bound errors, save/back |
| Tickets/list/thread | Subject, status/time, two-way messages with sender identity, compose/reply |
| Profile/settings | Identity details privacy, editable profile/contact/password/page ID, verification status |
| Staff dashboard | Pending identities/listings/reports, section links, audited moderation actions, search by private numeric code, permissions/tickets |
| Error/access pages | Not found/forbidden/restricted/expired with explanation and actual next action |

Global states: success and error announcements (role=status/alert), no results, no records, waiting moderation, rejected with reason, account restricted with end time, expired subscription with renewal, disabled actions with reason, loading button while submission in progress. Every form has labels and errors; no orphan placeholder-only controls. Private identity documents never appear in public markup.

## Native UI copy lock and components

Header: «کامیوا»، «آگهی‌ها»، «کارشناسی»، «نمایشگاه‌ها»، «پنل من»، «ثبت آگهی». Marketplace: «ماشین سنگین بعدی‌تان را اینجا پیدا کنید»، «بازار تخصصی خرید و فروش ماشین‌های سنگین»، «همه»، «کامیون»، «کشنده»، «تریلر»، «بارکش»، «جست‌وجو»، «برند»، «استان»، «حداقل قیمت (تومان)»، «حداکثر قیمت (تومان)»، «فقط کارشناسی‌شده»، «اعمال فیلترها»، «پاک کردن همه»، «جدیدترین»، «مقایسه». User-required test-mode notice is an intentional addition to distinguish seeded data and simulated payments.

Reusable pieces: global base/nav/footer, line icon, bound form field renderer, listing card, status badge, seller dashboard navigation, pagination, empty state, compare tray. Primary navy action, purchase/create amber action, secondary bordered action, destructive red text action. All app controls stay code-native; generated imagery is limited to real bitmap vehicle assets and concept documentation.

## URL and context contract

All named routes use namespace `market`: landing, home, listing_detail(pk), seller_page(slug), dashboard, listing_create, listing_edit(pk), listing_status(pk), favorites, favorite_toggle(pk), compare, compare_toggle(pk), subscriptions, checkout(pk), inspection_create(pk), inspections, inspection_detail(pk), report_create(pk), tickets, ticket_create, ticket_detail(pk), profile, verify, login, register, logout, staff.

Standard contexts: request.user, messages, form, listing/listings, page_obj. Optional contexts progressively enrich UI: categories, provinces, brands; seller; stats and quotas; plans; inspection(s); ticket(s); thread messages. Generic bound fields preserve functional forms independently from richer page-specific contexts. All mutations use CSRF-protected POST forms. GET is used for filtering/sorting/pagination only.

## Verification ledger (complete during rendered QA)

Compare desktop reference and latest browser screenshot with view_image. Check palette, type scale, RTL placement/filter rail, card anatomy/media framing, copy, gaps/radii and responsive single-column behavior. Check 360/768/1440 widths, light/dark, keyboard, forms, favorite/compare interactions. Record intentional differences and fixes here before handoff.

### Rendered review — 2026-09-14

Product manager compared both concept images and captured desktop/mobile pages. Approved: local Vazirmatn, navy/amber tokens, right-hand filter rail, three-column desktop cards, mobile single-column results, keyboard focus and both themes. Screenshots are retained privately in `artifacts/market-1440-light.png`, `market-1440-dark.png`, `market-360-dark.png`, `detail-360-light.png`, `ticket-768-dark.png` and `pro-panel-1440-light.png`.

Intentional differences from the concepts: test-stage banner; real database counts and sample prices; mobile filters grouped in a disclosure; selected comparison tray appears only when populated; fewer distinct sample vehicle images. Minimum and maximum price labels are distinct. Final expert prices and dates appear on inspected cards. Long comparison tables scroll inside their own region; the page itself does not overflow.

Verified in-browser: 360/768/1440 layouts; light/dark theme persistence across navigation; keyboard Tab from filter disclosure to labeled search with visible amber focus; expand and submit mobile filters; buyer login, favorites, comparison limit, details/contact link, paid test appraisal request and ticket replies; pro login, new pending listing, price update and pause with capacity release. No console errors or warnings were recorded in the recovered final tab. Staff/expert/MFA behavior has automated HTTP coverage; staff MFA enrollment is reserved for the administrator's own authenticator setup.

Remaining polish: some small counters use Latin digits, and operational dates use the Gregorian calendar. Vehicle years and monetary prices are Persian. A complete screen-reader audit and Jalali operational date conversion are not claimed.

### Focused product approval — 2026-09-15

User-requested removal of compulsory MFA setup is approved. Unenrolled staff proceed to normal panels; profile settings carry the explicit “فعال‌سازی اختیاری تأیید دومرحله‌ای” POST action and explain the authenticator requirement before enrollment. Direct navigation to the previous setup URL returns to profile without exposing a key. Existing enrolled accounts remain protected. No visual token or layout redesign is needed for this focused change.

### Redesign — 2026-10-20 (user-directed, Bama-style search-first)

The user rejected the previous look as too plain and chose a search-first marketplace direction with Bama.ir as reference. Tokens (navy/amber, Vazirmatn, softened light theme from phase 1) are unchanged; layout and components changed:

- Header: logo · central search (GET to the marketplace) · actions in one 76 px row, plus a 48 px category bar (all listings, root categories, sellers, inspection, plans). Mobile: search drops to a full-width second row; category bar scrolls horizontally.
- Landing: full-bleed hero photo from the licensed rotating pool with a right-weighted navy overlay, kicker, two-line headline, a search card (category, province, brand/model), trust points; then category tiles with live counts, a navy stats band, numbered inspection steps, plans and FAQ.
- Listing card: 4:3 photo, two-line title, spec chips (year, mileage, province), price with negotiation badge, optional expert price, footer with single-unit "time ago", views and 40 px icon actions (compare, favourite).
- Marketplace: category chips instead of underlined tabs, bordered results toolbar; 4 columns from 1400 px, filters become a grid above the results between 761 and 1100 px, disclosure below 760 px.
- Listing page spec tiles; navy footer with link columns; panel sidebar uses an amber-tinted active item.
- Implementation lives in `static/kamiwa/ui.css`, loaded after `app.css`.

Verified in the browser at 360, 768 and 1440 px, light and dark, with no horizontal overflow.

### v3 — 2026-10-20 (user-directed: indigo + lime, 3D, motion)

The user asked for a more attractive site with 3D imagery, animation and a new palette, keeping the backend. Chosen: royal indigo `#2B2D7C`, violet `#6C5CE7`, lime `#B8F35A` (CTA fills with dark text), page `#F5F6FB`; dark theme `#0E0F24`/`#17183A` with violet `#9A8CFF`. Motion level: moderate.

- 3D imagery: 46 Microsoft Fluent Emoji 3D images (MIT, notice in `static/kamiwa/3d/LICENSE.txt`) as 256 px WebP, rendered through `market/includes/emoji.html`.
- Landing: gradient hero (tilted photo from the licensed pool, floating 3D objects, info cards, two-row search), features, 3D category tiles, gradient stats with counters, 3D steps, plans with ribbon, FAQ, closing CTA.
- Global: gradient wordmark, glass header, gradient primary buttons, lime CTAs, violet focus ring, 20 px card radius; 3D icons on panel metrics, empty states and the login panel.
- Motion: scroll reveal (hero excluded, 1.5 s fallback), counting stats, floating objects; disabled under `prefers-reduced-motion`.
- Implementation: `static/kamiwa/v3.css` after `app.css` and `ui.css`; legacy tokens are remapped so every page adopts the palette.

Verified at 360 and 1440 px in light and dark with no horizontal overflow.

### Industrial — 2026-10-21 (user-directed: petrol green + sunset orange)

The user found the v3 indigo/lime palette and the 3D emoji childish and asked for a professional, industrial look. From the palettes shown they chose petrol green and sunset orange.

- Light: page `#F3F5F4`, surface `#FFFFFF`, primary petrol `#0C4F63`, deep petrol `#073B4C` (hero, stats, footer), accent orange `#FF7A1A` with dark text `#0B1F26` on it for contrast. Dark: page `#08171C`, surface `#0F242B`, light petrol `#6CC3D6` for emphasis text, same orange.
- Flat surfaces, 8/12 px radii, no glow or glass; hero and auth panel use a faint diagonal stripe texture and a 4 px orange rule.
- The 3D emoji (and `static/kamiwa/3d/`) are removed. `market/includes/emoji.html` keeps its parameters but now renders a line icon from `includes/icon.html` inside a square tile (`.glyph-sm|md|lg|xl`); the `glyph` filter maps old emoji names to icon names.
- Motion: no floating or hover lifts; only the scroll reveal and the stats counter remain.

### Luxe noir — 2026-10-24 (user-directed, sample A "Noir & Gold")

The user rejected the cinematic 3D direction (both the code-built and a downloaded truck model) and chose sample A of three static homepage concepts, without 3D.

- Palette: noir `#0A0A0A` / surface `#111111` / line `#262420`, ivory text `#F4F1EA`, muted `#9A968D`, champagne gold `#C8A96A` for the single main action and accents. Light variant: ivory `#F6F4EF` with black text and darker gold `#A88A4F`. Dark is the default (`theme.js`); a saved light choice wins.
- Square corners (2–4 px), hairline borders, no shadows or glow; thin gold line icons instead of icon tiles; light-weight large numerals for stats and metrics.
- Landing (`templates/market/landing.html`, classes `lx-*` in `static/kamiwa/luxe.css`): full-bleed graded truck photo hero with transparent header, segmented search bar, stat row, photo category tiles, feature columns, photo/steps split, plans with a featured Pro column, FAQ, photo CTA.
- Photography: the three local truck photos as WebP (`static/kamiwa/images/*.webp`, 1600 and 800 px).
