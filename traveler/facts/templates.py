"""Sentence templates: 2-4 phrasings per fact type, picked by the hop seed. English only."""

from __future__ import annotations

TEMPLATES: dict[str, tuple[str, ...]] = {
    # The place is named once, in the opening fact; later statements mostly say "here".
    "time.now": (
        "It's {hhmm} {near}, {when}.",
        "Local time {near}: {hhmm}, {when}.",
        "{near}, it's {hhmm}, {when}.",
    ),
    "sun.daylight_left": (
        "There's about {left} of daylight left here; sunset is at {sunset}.",
        "Sunset here is at {sunset}, about {left} from now.",
    ),
    "sun.before_rise": (
        "The sun rises here at {sunrise}, about {wait} from now.",
        "It's still dark here. Sunrise is at {sunrise}, in about {wait}.",
    ),
    "sun.after_set": (
        "The sun set here at {sunset}, about {ago} ago.",
        "Night has fallen here. The sun went down at {sunset}.",
    ),
    "sun.day_length": (
        "Today brings {length} of daylight here, from {sunrise} to {sunset}.",
        "Daylight here today runs from {sunrise} to {sunset}, {length} in all.",
    ),
    "sun.elevation_up": (
        "The sun is {deg}° above the horizon here right now.",
        "Right now the sun sits {deg}° above the horizon.",
    ),
    "sun.elevation_down": (
        "The sun is {deg}° below the horizon here right now.",
        "Right now the sun is {deg}° below the horizon.",
    ),
    "journey.distance": (
        "That flight covered about {km} km from {origin}.",
        "I just flew about {km} km to get here from {origin}.",
        "About {km} km lie between here and {origin}, where I just was.",
    ),
    "journey.clock": (
        "Clocks here are {hours} {direction} {origin}.",
        "Crossing from {origin} moved the clock {hours} {direction_short}.",
    ),
    "journey.same_clock": (
        "Clocks here show the same time as {origin}.",
        "No time change on this hop: {origin} and here share a clock.",
    ),
    "journey.stop": (
        "This is stop number {n} on the journey.",
        "Stop number {n}. The journey keeps going.",
    ),
    "weather.now": (  # can open a stay, so every phrasing names the place
        "It's {c}°C ({f}°F) with {cond} {near} right now.",
        "Right now it's {c}°C ({f}°F) {near}, with {cond}.",
        "Weather {near} at the moment: {cond}, {c}°C ({f}°F).",
    ),
    "weather.wind": (
        "The wind here is blowing at about {kmh} km/h ({mph} mph).",
        "Wind right now: about {kmh} km/h ({mph} mph).",
    ),
    "quake.strongest": (
        "The strongest earthquake within {radius} km of {city} this week was magnitude {mag}, {place}.",
        "This week's biggest quake within {radius} km of {city}: magnitude {mag}, {place}.",
    ),
    "quake.none": (
        "No earthquakes of magnitude {min} or more have struck within {radius} km of {city} this week.",
        "The ground has been quiet here: no magnitude {min}+ quakes within {radius} km this week.",
    ),
    "onthisday.event": (
        "On this date in {year}: a moment in {place_pos} history involving {title}.",
        "{year}, on this date, {place} made Wikipedia's On This Day, in an entry on {title}.",
    ),
    "nearby.place": (
        "Nearby: {title}, about {km} km from here.",
        "{title} is about {km} km from here.",
        "About {km} km away is {title}.",
    ),
    "holiday.today": (
        "Today is {name} in {country}.",
        "It's a public holiday in {country} today: {name}.",
    ),
    "holiday.soon": (
        "The next public holiday in {country} is {name}, in {days} days.",
        "{name} is {days} days away, the next public holiday in {country}.",
    ),
    "money.fx": (
        "One US dollar buys about {rate} {currency_name} today.",
        "Today's rate: US$1 is about {rate} {currency_name} ({currency}).",
    ),
    "country.capital": (
        "{country}'s capital is {capital}.",
        "The capital of {country} is {capital}.",
    ),
    "country.languages": (
        "{country}'s official {lang_word}: {languages}.",
        "Official {lang_word} of {country}: {languages}.",
    ),
    "country.currency": (
        "{country} pays in the {currency_name} ({currency}).",
        "The currency here is the {currency_name} ({currency}).",
    ),
    "country.drives": (
        "Traffic in {country} drives on the {side}.",
        "In {country}, cars keep to the {side}.",
    ),
    "country.calling": (
        "Phone numbers in {country} start with {calling_code}.",
        "To call {country} from abroad, dial {calling_code}.",
    ),
    "evergreen.text": ("{text}",),
}
