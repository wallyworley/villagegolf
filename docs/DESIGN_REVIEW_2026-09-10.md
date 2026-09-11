# Village Fairways — design review

Reviewed September 10, 2026, at http://127.0.0.1:8091/.

## Recommendation

Keep the green identity and give the website the feeling of a welcoming local
clubhouse: clear, comfortable, and connected to the actual places residents play.
The current version has a coherent foundation. Its biggest weaknesses are small
supporting text, an unnecessarily narrow desktop layout, a tall repeated header,
and too many similarly styled boxes competing for attention.

This is design guidance, not an implementation change. The live desktop booking
form, synthetic search results, course finder, and course details were inspected.
The request form and course detail modal were also inspected in Chrome's 400px
responsive viewport. This was not a physical iPhone/Safari test or full
accessibility audit. A smaller agent independently reviewed the HTML and CSS.
No booking, cancellation, email, or saved-location change was submitted.

## What is already working

- Forest green, pale neutral backgrounds, and the restrained gold accent fit golf.
- The consistent line icons are quieter and more coherent than mixed emoji.
- The large date numbers and named time choices are easy to understand.
- Selected options are visibly differentiated, and the main search button is clear.
- Area names such as North of 466 and Between the 6s give useful local context.
- Course names, addresses, real map imagery, and favorites are a useful foundation.
- The course modal fits within the inspected phone viewport and has a clear close button.

## Highest-impact changes, in order

### 1. Make the layout genuinely responsive

**Observed:** On desktop the site remains a narrow central column with very large
empty margins. On the course page, desktop rules put cards into two columns inside
that narrow container, leaving each card cramped.

**Cause in source:** `templates/index.html` loads `experience.css` at line 10,
before the embedded styles. Later `.wrap { max-width:480px }` and two-column
`.app-nav` rules override the external stylesheet's desktop width and navigation
rules. Media queries do not increase selector specificity.

**Direction:** Establish one predictable stylesheet order before adding more
overrides. Use about 1040–1120px of content width on desktop, while keeping form
fields at a comfortable reading width. Place booking choices beside a compact
selection summary; put the course list beside a larger map. On phones use one
column and a clear List / Map switch. This alone should make the site feel much
more finished.

### 2. Increase readability and reduce decorative weight

**Observed/source:** Course descriptions, field help, weather timestamps, and
route metadata often use 10–13px text. Entire locked sections are faded to 60%
opacity. Labels, nested panels, cards, borders, and shadows all compete visually.

**Direction:** Aim for 17–18px body text, 15–16px labels, 14px supporting text,
22–26px section headings, and 30–36px main headings. These are proposed design
sizes, not WCAG minimum font requirements. Use sentence case for most labels,
comfortable line spacing, and dark secondary text. Reserve small metadata for
information that truly is secondary. Keep one level of strong card containment;
use spacing and subtle dividers inside it.

Keep locked-step explanations readable. Instead of fading whole cards, show a
compact next-step message, such as “Choose a date to select your golfers.” Retain
visible selection checkmarks and keyboard focus, rather than relying on green alone.

Use 48–52px primary controls and at least 44px interactive targets where practical.
The present map pins are 27px. Increasing their hit areas must be paired with
clustering or a list alternative so neighboring targets do not overlap.

These choices align with W3C guidance on contrast, readable presentation, and
easier controls for older users. Avoid treating all residents as having the same
needs; test with people who differ in vision, device habits, and golf experience.

### 3. Give the brand warmth without delaying the task

**Observed:** A large centered gradient header repeats above every working view.
At the inspected phone size, the header and two navigation rows consume much of
the initial viewport before the actual task begins. The VF initials alone do
little to establish a distinctive local identity.

**Direction:** Use a compact signed-in header: a modest golf-flag or fairway mark,
Village Fairways wordmark, and a clearly labeled account control. Keep the existing
green as the primary action color. Consider a slightly warmer off-white background.

Use one excellent, permission-cleared photograph of an actual local course on the
welcome/login screen, with a short line such as “Your next round starts here.”
Photographs of specific courses can also help in course details. Avoid placing a
large photographic banner above every booking form or downloading 62 full images
for the directory. The photograph should reinforce place, not obscure text.

Suggested palette:

| Role | Starting color | Application |
| --- | --- | --- |
| Forest | `#14532D` | Primary buttons, selected items, brand |
| Warm ivory | `#F7F5EF` | Page background |
| White | `#FFFFFF` | Main content surfaces |
| Soft sage | `#E9F1EA` | Selected backgrounds and quiet highlights |
| Charcoal green | `#16211A` | Main text |
| Dark muted green | `#55615A` | Supporting text |
| Brass | `#B3872F` | Small accents; avoid small brass text on white |

Keep the system sans-serif for controls and body text. A restrained serif wordmark
could add character, but typography consistency matters more than adding a new font.
Verify all final color pairings in both light and dark modes.

### 4. Organize around a resident's next action

**Observed:** Travel settings, saved booking presets, and Account sit after the
course directory. Email settings appear after tee-time search results. They are
difficult to find and extend pages that already require substantial scrolling.

**Direction:** Keep four task destinations: **Find tee times**, **My golf**,
**Advance requests**, **Courses**. Use a compact persistent navigation bar on
phones with text labels and icons; ensure it never covers fields or modal actions.
Desktop can use a single navigation row. Put profile, email, and travel preferences
under an obvious account menu. Place a saved-group picker beside golfer selection,
where it is useful, and rename “Booking presets” to “Saved groups & preferences.”

Make an upcoming-round card prominent in My golf:

```text
Your next round
Friday, September 11 · 8:15 AM
Silver Lake · 2 golfers

[Course & cart directions]   [Add to calendar]
```

This is an illustrative layout, not a real reservation. If travel time is available
from the selected origin, add a clearly labeled estimated departure time that
accounts for the arrival buffer. Show how it was calculated and allow changes.
If there is no upcoming round, show one short invitation to find a tee time.

### 5. Make course discovery the feature residents remember

**Observed:** Filtering to North of 466 leaves the map showing the wider community;
the 14 visible markers bunch up near the top. Numbered circles overlap and do not
explain the places without opening details. The phone map begins near the bottom
of the initial viewport.

**Direction:** Fit the map to filtered results, provide zoom controls and “Show all,”
cluster nearby courses, and show the selected course name. Offer a larger map view
with the search and area controls within easy reach. Keep the list equally usable.
Use actual landmarks and roads to build recognition, and preserve the new local
area names. Labels such as “Near Lake Sumter Landing” should only be shown when
the relationship is verified; matching an area search does not establish proximity.

A useful course card hierarchy is:

```text
Silver Lake                                      ☆
Executive · 9 holes
Silver Lake area · North of 466

[View course]                      [Cart directions]
```

Move the full street address into details or a secondary line when space allows.
Show route duration only when actually calculated; distinguish it from straight-line
distance. Explain course difficulty levels in words if a reliable official meaning
is available. Do not display a bare “Level 2” as though everyone understands it.

In course details, type and area currently appear in the heading, summary, and
again in separate statistic tiles. Remove duplicates. Put a course photograph or
location map first, followed by a compact “9 holes · Par 29” line. Make **Get cart
directions** the primary action, with an origin selector. Keep Call, Official course
page, and scorecard as secondary links. Use **Set starting point** with an explicit
pin confirmation instead of relying on an undiscoverable long press.

### 6. Simplify booking and requests visually

**Observed:** Booking is a long chain of cards. Optional exact-time controls always
occupy space. Search-result rows show a visible Location button but the main booking
action depends on knowing to click the time/course area. Every result repeats a
forecast refresh timestamp.

**Direction:** Group booking into **When**, **Who**, and **Preferences**, followed
by a compact persistent summary and the search action. Keep the friendly time
presets, with exact times behind an “Choose an exact time range” disclosure.
Do not add a rigid multi-page wizard unless resident testing shows it helps.

Give each result a clear hierarchy: large time, prominent course name, party
availability, then a single weather line. Add a labeled **Choose this time** action
that opens the existing confirmation step. Keep **Course & directions** secondary.
Show the forecast update time once above the result list, with detail on demand.

For advance requests, use the same input styling and spacing as booking. Add
visible group headings for date/group, time preferences, ranked courses, and final
review. Replace the ambiguous “Preference: Course / Time” with “What matters most?”
and understandable option labels. Add a final summary before submission.

### 7. Make entry and feedback feel dependable

Explain that the website uses the resident's existing thevillages.net account and
golf PIN, and why those are requested. Include an accessible Show password control,
plain help, and real button/link semantics for registration. Make the website's
independent identity clear without implying official affiliation.

Use specific feedback, such as “Finding tee times for your group…” and “Your
reservation is confirmed.” Keep the existing distinction between a confirmed
result and an outcome that still needs verification. Visual polish must not hide
that important information. Do not use a bright success treatment for an unknown
booking outcome.

## Practical sequence for Claude or the next implementation pass

1. Fix stylesheet order and desktop layout; increase the core text scale.
2. Shrink the signed-in header; establish consistent navigation and action hierarchy.
3. Improve the course map, course cards, and details; relocate account/travel settings.
4. Refine booking/results and advance-request grouping using the same components.
5. Add the local photography and final brand polish.

Keep backend booking and routing behavior stable during the visual pass. Validate
at 360–400px phone widths, tablet and desktop, keyboard-only operation, and enlarged
text/zoom. Test the real Safari version as well as Chrome emulation. Check focus
visibility, dialogs, sticky elements, long names, empty lists, and network failures.

Ask a small group of residents to find a course in an unfamiliar area, find a
morning tee time for two golfers, and locate their next round's cart directions.
Observe where they hesitate and which terms they use. My first priorities would
be readability, map orientation, and shorter task paths; photography comes after
those work well.

## References

- [W3C: Older users and web accessibility](https://www.w3.org/WAI/older-users/)
- [W3C: Contrast minimum](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html)
  — ordinary text generally needs 4.5:1; this review did not certify all current pairs.
- [W3C: Target size enhanced](https://www.w3.org/WAI/WCAG22/Understanding/target-size-enhanced.html)
  — 44×44 CSS pixels is the enhanced Level AAA target criterion, with exceptions;
  it is not being presented here as the universal WCAG AA minimum.
