"""
The chest module's own calibration steps: the way into the gifts panel and the
three marks that describe one chest.

They follow the shared steps (store, profiles) in the wizard, and are in the
order of a real collection: each one leaves the game on the screen the next one
expects.
"""

from core.calibration import Step

STEPS = (
    Step("clan_button", "click", "Clan button",
         "Click the button that opens the CLAN menu.",
         acts=True),
    Step("gift_button", "click", "Gifts button",
         "Inside the clan menu, click GIFTS.",
         acts=True),
    Step("gifts_tab", "click", "Gifts tab",
         "Click the standard gifts tab.",
         acts=True),
    Step("triumphal_gifts_tab", "click", "Triumphal gifts tab",
         "Click the triumphal gifts tab.",
         acts=True),
    Step("chest_area", "area", "Chest text",
         "Mark the area where the CHEST NAME, player, and source appear.\n"
         "Include the TIME LEFT counter on the right: it is what dates the chest.\n"
         "Mark the FIRST chest of the list only — the others are derived from it.\n"
         "This text is saved to the database — leave some margin."),
    Step("open_button_area", "area", "Search area for 'Open' button",
         "Mark the area where the OPEN chest button appears, for that same first chest.\n"
         "Can be generous: the button is located by image matching inside it."),
    # Never pressed by the wizard (`acts` stays off): clicking it consumes a real
    # chest that nothing would record.
    Step("open_button", "region", "Open button",
         "Mark only the button to open the chest, tightly.\n"
         "This image is the reference searched for each chest.\n"
         "It is never clicked automatically: that would open a chest nothing records."),
)

# What a collection cannot start without. The two tab steps are left out on
# purpose: an uncalibrated tab is skipped with a warning, as it always was.
REQUIRED_STEPS = ("clan_button", "gift_button", "chest_area", "open_button_area", "open_button")
