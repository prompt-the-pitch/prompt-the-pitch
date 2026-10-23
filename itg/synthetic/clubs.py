"""Fictional clubs and squads.

Every name is invented so that no real club, player or league is represented.
Squads are generated from a fixed seed, so the same club always has the same players.
"""

from __future__ import annotations

import random
import re
import unicodedata

from itg.schema import Attributes, Player, Position, Team, TeamStyle

# Formation roles: (role, base x, base y, position group), in the team's own frame
# where the team attacks towards x = 105.
FORMATIONS: dict[str, list[tuple[str, float, float, Position]]] = {
    "4-3-3": [
        ("GK", 5, 34, "GK"),
        ("RB", 30, 8, "DF"), ("RCB", 24, 24, "DF"), ("LCB", 24, 44, "DF"), ("LB", 30, 60, "DF"),
        ("RCM", 50, 22, "MF"), ("CDM", 42, 34, "MF"), ("LCM", 50, 46, "MF"),
        ("RW", 75, 10, "FW"), ("ST", 82, 34, "FW"), ("LW", 75, 58, "FW"),
    ],
    "4-2-3-1": [
        ("GK", 5, 34, "GK"),
        ("RB", 30, 8, "DF"), ("RCB", 24, 24, "DF"), ("LCB", 24, 44, "DF"), ("LB", 30, 60, "DF"),
        ("RDM", 42, 26, "MF"), ("LDM", 42, 42, "MF"),
        ("RAM", 66, 12, "MF"), ("CAM", 66, 34, "MF"), ("LAM", 66, 56, "MF"),
        ("ST", 82, 34, "FW"),
    ],
    "4-4-2": [
        ("GK", 5, 34, "GK"),
        ("RB", 30, 8, "DF"), ("RCB", 24, 24, "DF"), ("LCB", 24, 44, "DF"), ("LB", 30, 60, "DF"),
        ("RM", 60, 10, "MF"), ("RCM", 50, 27, "MF"), ("LCM", 50, 41, "MF"), ("LM", 60, 58, "MF"),
        ("RST", 80, 28, "FW"), ("LST", 80, 40, "FW"),
    ],
}

_CLUB_SPECS = [
    dict(id="expected-ls", name="Expected Ls", short_name="EXL",
         colors=("#1B4F9C", "#FFFFFF"), formation="4-3-3", quality=77,
         style=TeamStyle(possession=0.75, pressing=0.6, directness=0.25)),
    dict(id="press-gang", name="Press Gang", short_name="PRG",
         colors=("#C8102E", "#111111"), formation="4-2-3-1", quality=76,
         style=TeamStyle(possession=0.5, pressing=0.85, directness=0.45)),
    dict(id="carb-loaders", name="Carb Loaders", short_name="CRB",
         colors=("#00A3AD", "#F2C14E"), formation="4-4-2", quality=73,
         style=TeamStyle(possession=0.35, pressing=0.4, directness=0.75)),
    dict(id="benchwarmers-fc", name="Benchwarmers FC", short_name="BEN",
         colors=("#5B2C83", "#E0E0E0"), formation="4-2-3-1", quality=75,
         style=TeamStyle(possession=0.55, pressing=0.55, directness=0.45)),
]

# (first names, last names) per nationality.
_NAMES: dict[str, tuple[list[str], list[str]]] = {
    "ENG": (["Callum", "Reece", "Toby", "Harvey", "Ollie", "Jamie", "Kieran", "Lewis"],
            ["Ashworth", "Pemberton", "Holloway", "Rudge", "Fairley", "Thackeray", "Brindle", "Coombes"]),
    "FRA": (["Théo", "Bastien", "Yanis", "Mathis", "Enzo", "Loïc", "Rayan", "Adrien"],
            ["Valette", "Morvan", "Delcourt", "Ferrand", "Lebrun", "Castagne", "Rouvière", "Tessier"]),
    "ESP": (["Iker", "Álvaro", "Unai", "Pablo", "Marcos", "Rubén", "Sergio", "Hugo"],
            ["Salcedo", "Arrieta", "Montoya", "Quintana", "Velasco", "Ibarra", "Lozano", "Cuesta"]),
    "BRA": (["Thiago", "Matheus", "Caio", "Rafael", "Davi", "Igor", "Vinícius", "Léo"],
            ["Barbosa", "Cardoso", "Teixeira", "Moraes", "Pires", "Assunção", "Brandão", "Fontes"]),
    "NGA": (["Chidi", "Tunde", "Emeka", "Femi", "Kelechi", "Sola", "Obinna", "Dayo"],
            ["Okonkwo", "Adeyemi", "Nwosu", "Balogun", "Eze", "Akintola", "Obi", "Uche"]),
    "GER": (["Jonas", "Lukas", "Felix", "Niklas", "Jannik", "Moritz", "Tim", "Paul"],
            ["Brenner", "Kessler", "Hartwig", "Vogt", "Lindemann", "Ruhl", "Steiner", "Albrecht"]),
    "NED": (["Daan", "Sem", "Bram", "Lars", "Thijs", "Ruben", "Jesse", "Milan"],
            ["de Vries", "Bakker", "Visser", "van Dijkhuis", "Mulder", "Brouwer", "Kuipers", "Hendriks"]),
    "JPN": (["Haruto", "Sota", "Ren", "Kaito", "Yuto", "Riku", "Daiki", "Takumi"],
            ["Sakamoto", "Kimura", "Fujita", "Matsuda", "Okada", "Ishikawa", "Nakano", "Ueda"]),
    "SEN": (["Moussa", "Ibrahima", "Cheikh", "Pape", "Abdou", "Lamine", "Ousmane", "Babacar"],
            ["Diallo", "Ndiaye", "Sow", "Faye", "Camara", "Gueye", "Mbaye", "Sarr"]),
    "ARG": (["Facundo", "Lautaro", "Nahuel", "Joaquín", "Gonzalo", "Tomás", "Bruno", "Franco"],
            ["Ferreyra", "Benítez", "Ledesma", "Ojeda", "Sosa", "Villalba", "Acuña", "Ríos"]),
}

# Squad of 18: position group -> (count, shirt numbers).
_SQUAD = {"GK": (2, [1, 13]), "DF": (6, [2, 3, 4, 5, 15, 22]),
          "MF": (6, [6, 8, 10, 14, 16, 18]), "FW": (4, [7, 9, 11, 19])}

# Attribute offsets from the club quality, per position group.
_PROFILES: dict[str, dict[str, int]] = {
    "GK": dict(pace=-20, passing=-12, dribbling=-25, finishing=-45, defending=-30, goalkeeping=6, stamina=-5),
    "DF": dict(pace=-2, passing=-6, dribbling=-12, finishing=-25, defending=8, goalkeeping=-60, stamina=2),
    "MF": dict(pace=-2, passing=8, dribbling=2, finishing=-10, defending=-5, goalkeeping=-60, stamina=6),
    "FW": dict(pace=6, passing=-4, dribbling=6, finishing=9, defending=-25, goalkeeping=-60, stamina=-2),
}


def _slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def _build_team(spec: dict, used_names: set[str]) -> Team:
    rng = random.Random(spec["id"])
    players: list[Player] = []
    for group, (count, numbers) in _SQUAD.items():
        for i in range(count):
            while True:
                nationality = rng.choice(list(_NAMES))
                first, last = (rng.choice(pool) for pool in _NAMES[nationality])
                # Unique surnames keep commentary unambiguous ("Castagne" means one player).
                if last not in used_names:
                    break
            used_names.add(last)
            # The first players of each group are the starters: slightly better.
            bonus = 3 if i < count // 2 + 1 else -2
            attrs = {
                name: max(30, min(96, round(spec["quality"] + offset + bonus + rng.gauss(0, 5))))
                for name, offset in _PROFILES[group].items()
            }
            players.append(Player(
                id=f"{spec['short_name'].lower()}-{numbers[i]}-{_slug(last)}",
                name=f"{first} {last}",
                short_name=f"{first[0]}. {last}",
                number=numbers[i],
                position=group,
                nationality=nationality,
                attributes=Attributes(**attrs),
            ))
    return Team(
        id=spec["id"], name=spec["name"], short_name=spec["short_name"],
        colors=spec["colors"], formation=spec["formation"], style=spec["style"],
        players=players,
    )


def _build_all() -> dict[str, Team]:
    used: set[str] = set()
    return {spec["id"]: _build_team(spec, used) for spec in _CLUB_SPECS}


CLUBS: dict[str, Team] = _build_all()


def starting_lineup(team: Team) -> tuple[list[tuple[str, Player]], list[Player]]:
    """Return ([(role, player)] for the starting XI, bench) following the team formation."""
    remaining = {group: [p for p in team.players if p.position == group] for group in _SQUAD}
    starters: list[tuple[str, Player]] = []
    for role, _, _, group in FORMATIONS[team.formation]:
        starters.append((role, remaining[group].pop(0)))
    bench = [p for group in remaining.values() for p in group]
    return starters, bench
