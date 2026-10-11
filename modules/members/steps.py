"""
The members module's own calibration steps: the way from the clan menu to the
list of members.

The clan button is the chest module's `clan_button` - a step is known by its
name, so it is calibrated once, for both. These follow the chest steps in the
wizard, which leave the clan window open: the Members entry is in its menu.
"""

from core.calibration import Step

STEPS = (
    Step("clan_button", "click", "Clan button",
         "Click the button that opens the CLAN menu.",
         acts=True),
    Step("clan_members_button", "click", "Clan: Members",
         "Inside the clan menu, click MEMBERS.\n"
         "Open the clan menu first if it is not on screen.",
         acts=True),
    Step("clan_members_list", "area", "Clan: members list",
         "Mark the area of the LIST OF MEMBERS (the rows with name and power).\n"
         "The game usually sends every member at once; the list is only scrolled,\n"
         "inside this area, when some of them did not come.",
         optional=True),
)

REQUIRED_STEPS = ("clan_button", "clan_members_button")
