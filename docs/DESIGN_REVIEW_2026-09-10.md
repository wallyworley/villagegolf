# Village Fairways — a simpler website experience

Revised September 11, 2026. This replaces the earlier design recommendations in
this file. The existing filename is retained so links from Claude and Codex keep working.

## Direction

Keep the useful features, including golf cart directions, and simplify how residents
reach them. The goal is a mobile website that feels as straightforward as an app:
familiar navigation, readable screens, and an obvious next action.

Directions belong in this product. They answer the same practical need as booking:
“Where am I playing, and how do I get there?” The added capability does not need its
own top-level destination or a large collection of settings on the course page.

This pass should refine the existing website. A new dashboard, major rebranding,
additional features, and a native iOS application are outside the recommended scope.
Keep the existing booking, advance-request, reservation, and routing behavior stable.

## What this review is based on

The September 10 review covered the desktop booking form, synthetic search results,
course finder, and course details, plus the request form and course details in a
400px Chrome phone preview. The September 11 follow-up inspected the updated local
course finder and directions entry points at http://127.0.0.1:8091/ and their source.

The newer map already supports zooming, dragging, and resetting the view. Preserve
those improvements; adding zoom controls is no longer an outstanding recommendation.
The follow-up did not retest live route accuracy or physical iPhone behavior. No real
booking, cancellation, email, or saved-location change was submitted for this review.

## Keep four familiar destinations

| Destination | Its purpose |
| --- | --- |
| Book Tee Time | Find an available time and book it |
| My Tee Times | See confirmed reservations and get to the course |
| Advance Requests | Submit preferences and see pending requests |
| Courses | Find a course, understand its location, and get directions |

Rename “Requests” to “Advance Requests” to explain its purpose. Keep confirmed
reservations and pending requests visibly distinct.

Use consistent navigation with text labels. On phones, a compact persistent bar
should keep these destinations reachable without scrolling back to the top. Allow
labels to wrap when needed; do not shrink them to fit. Desktop can use one horizontal
row. Put a clearly labeled Account control in the compact signed-in header.

Do not add a separate Home, Dashboard, or Directions tab in this pass.

## Make cart directions one clear action

### Where the action belongs

Put **Cart directions** on each reservation and in course details. From a reservation,
the destination should already be selected. Residents should not have to search for
the same course again.

The intended path is:

**My Tee Times → next reservation → Cart directions → route preview.**

Within My Tee Times, put the next upcoming reservation first. Improve the existing
reservation card rather than building a separate dashboard.

### Replace competing route buttons

Course details currently give Call, Official page, Route from saved location, and
Route from current location similar visual emphasis. Replace the two route buttons
with one prominent **Cart directions** action. Keep calling and the official page
as secondary links.

After tapping directions, show a compact starting-point row:

```text
Cart directions to Silver Lake
From: Saved starting point                    Change
```

Use the saved starting point when one exists, and make it visible and easy to change.
If none exists, offer **Use current location** or **Choose on map**. Request location
only after the resident chooses it. Allow a one-time starting point without requiring
it to be saved. Saving a location should be an explicit choice.

### Keep the route screen focused

Show the destination, starting point, map, mileage, and estimated cart travel time.
Put written turns behind **Show directions**. Put provider details and network date
under **About this route**, while retaining a short visible label such as “Cart route
preview · estimated travel time.” Do not imply continuous spoken navigation.

Keep errors and uncertainty visible. An unavailable route should provide an obvious
way to change the starting point or try again, without presenting a failed calculation
as a valid route.

### Leave room for future turn-by-turn navigation

Walter plans to add turn-by-turn directions later. Keep that future experience
inside this same course-and-route flow. A resident should still reach it from
their reservation or a course, with the destination already selected.

When navigation is ready, the route preview can offer an explicit start action.
The active screen should emphasize the next turn, remaining distance, and an easy
way to end navigation. Keep planning controls and provider details out of that
active screen. Do not show a start-navigation button until that behavior exists.
Validate navigation on residents' actual phones before choosing whether any part
needs a native companion. The current website remains the main product.

## Put settings where people expect them

The current Courses page continues from the course directory into travel preferences,
booking presets, and account maintenance. Email settings appear after booking results.
Move these controls to the places where they are relevant:

| Control | Recommended location |
| --- | --- |
| Saved starting point and arrival preference | Account → Travel preferences |
| Email and golfer-list maintenance | Account |
| Saved group picker | Beside golfer selection in booking |
| Save or manage group preferences | A secondary action beside that picker |
| Course favorites | Courses, beside course names and in its filters |

Keep optional travel settings collapsed within Account until requested. Use plain
labels such as “Arrive before tee time” instead of “Arrival buffer.” If maximum cart
time remains available, explain its actual effect; do not imply it filters results
unless that behavior is implemented.

A resident should be able to book or preview a route without first configuring a
preferences page.

## Make existing screens calmer

### Course details

Type and area currently appear several times in the course popup. Show each once.
Replace the collection of repeated tiles with a short, readable summary:

```text
Silver Lake
Executive · 9 holes · Par 29
Silver Lake area · North of 466
679 Rainbow Boulevard

[Cart directions]
Call course       Official course page
```

This is an illustrative layout using the reviewed course data. Keep other useful
facts available below it. Explain numbered difficulty levels if there is a reliable
official definition; avoid making residents interpret an unexplained number.

### Course finder

Keep course search, the local area names, favorites, and the interactive map. Keep
the course list usable as an alternative to selecting pins. Offer **Show results on
map** or fit newly filtered results into view so a narrow search does not leave its
courses lost in a community-wide map. Preserve a resident's deliberate pan/zoom
until they change the search or request a reset.

Avoid overlapping pin targets. Use clustering or another clear selection method
where needed. Provide an explicit **Choose starting point** mode with confirmation;
a long press can remain a shortcut, but it should not be the only discoverable method.

### Booking and search results

Retain the current date, golfer, course, and time choices. Group them clearly and
keep the saved-group picker near the golfers. Put optional exact-time fields behind
**Choose an exact time range**. Avoid turning the form into a new multi-page wizard.

For each result, emphasize time and course, followed by availability and a short
weather line. Add a clear **Choose this time** button that opens the existing booking
confirmation. Keep **Course & directions** secondary. Show the forecast update time
once for the result list rather than repeating it in every row.

### Advance requests

Use the same typography, inputs, and spacing as booking. Group existing fields into
date/group, time preferences, and ranked courses. Replace “Preference: Course / Time”
with “What matters most?” and understandable choice labels. Keep a readable summary
before final submission and preserve the distinction between submitted and confirmed.

## Polish the appearance without a redesign

Keep the forest green, pale backgrounds, restrained brass accent, line icons, and
system font. Consistency and comfortable spacing will provide more immediate value
than a new logo, font family, or photographic treatment.

- Aim for 17–18px body text, 15–16px labels, and about 14px supporting text. These are
  proposed design sizes, not WCAG minimum font requirements.
- Use 48–52px primary controls and comfortably sized secondary targets. Keep visible
  focus and selected-state indicators. Make important explanations readable instead
  of fading whole sections.
- Shorten the signed-in header so the main task appears sooner on a phone.
- Reduce nested boxes, heavy borders, and repeated shadows. Use spacing and dividers
  to group related information.
- Fix the desktop stylesheet ordering that lets the embedded 480px `.wrap` rule
  override the wider layout in `experience.css`. Give the content room to breathe;
  keep forms at a comfortable width. A larger map beside the course list can use
  desktop space without introducing another screen.

Local photography is optional polish for a later pass. It is not required to make
this website attractive or easy to use, and should not delay these improvements.

## Implementation order

1. **Readability and layout:** fix stylesheet order, increase text size, shorten the
   header, and make navigation consistent.
2. **Directions flow:** one main action, visible/changeable starting point, focused
   route preview, and direct access from each reservation.
3. **Reduce clutter:** relocate settings, remove repeated course details, and collapse
   optional controls.
4. **Refine existing forms:** clear result actions and consistent request-field groups.

Implement these as presentation and interaction improvements around the existing
backend. Preserve search-context validation, booking confirmation, account isolation,
and handling of unknown outcomes. Do not use visual success feedback for an operation
whose outcome is still unconfirmed.

## How to judge whether it is simpler

Ask a few residents to complete three tasks without coaching:

1. Find a morning tee time for two golfers.
2. Locate a course in an unfamiliar area.
3. Open an upcoming reservation and get cart directions.

Watch where they pause, scroll past an action, or need an explanation. Check that
settings are optional, the next action is evident, and returning to a previous
screen preserves useful choices without leaving invalid booking results selectable.

Validate at phone, tablet, and desktop sizes, with enlarged text and keyboard use.
Test Safari on an actual iPhone as well as Chrome. Check dialogs, persistent
navigation, long course names, empty results, and route failures. These are review
criteria for the next implementation; they have not all been verified in this pass.

## Guidance for Claude

Refine the existing website using this document. Keep cart directions and the four
main destinations. Prioritize fewer competing actions, readable text, and settings
that remain out of the way. Build no additional dashboard or top-level feature in
this pass. Show the revised flows with synthetic booking data before release.

Accessibility references consulted during the original review:
[W3C guidance for older users](https://www.w3.org/WAI/older-users/),
[contrast guidance](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html),
and [enhanced target size](https://www.w3.org/WAI/WCAG22/Understanding/target-size-enhanced.html).
The 44×44 CSS-pixel target recommendation is the enhanced Level AAA criterion,
with exceptions; this document does not certify WCAG conformance.
