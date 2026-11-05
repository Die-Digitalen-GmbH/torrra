import pytest

from torrra.utils.video import humanize_title


@pytest.mark.parametrize(
    ("stem", "title"),
    [
        # movies
        ("Aftersun.2022.720p.WEBRip.800MB.x264-GalaxyRG", "Aftersun (2022)"),
        ("Antitrust (2001) 720p BluRay.x264 SUJAIDR", "Antitrust (2001)"),
        (
            "Star.Wars.Episode.4.A.New.Hope.1977.1080p.BrRip.x264.BOKUTOX.YIFY",
            "Star Wars Episode 4 A New Hope (1977)",
        ),
        # words that look like tags stay in the title
        (
            "The Darjeeling Limited (2007) (1080p BluRay x265 Silence)",
            "The Darjeeling Limited (2007)",
        ),
        ("Spider-Man.No.Way.Home.2021.1080p", "Spider-Man No Way Home (2021)"),
        ("Kill.Bill.Vol.2.2004.1080p.BluRay", "Kill Bill Vol 2 (2004)"),
        ("Charlotte's.Web.2006.1080p", "Charlotte's Web (2006)"),
        # years that are part of the title
        ("Blade.Runner.2049.2017.1080p.BluRay.x264", "Blade Runner 2049 (2017)"),
        ("2001.A.Space.Odyssey.1968.1080p.BluRay", "2001 A Space Odyssey (1968)"),
        ("1917.2019.1080p", "1917 (2019)"),
        ("2012.1080p.BluRay", "2012"),
        # episodes, with the year in any position
        ("Big.Mouth.S01E01.(2022).1080p.WEBRIP.HEVC.AAC2.0", "Big Mouth S01E01 (2022)"),
        (
            "Big.Mouth.2022.S01E13.KOREAN.720p.DSNP.WEBRip.x264-GalaxyTV",
            "Big Mouth S01E13 (2022)",
        ),
        (
            "Furious (2026) - S01E02 - My life had stood - a Loaded Gun "
            "(1080p HULU WEB-DL x265 Ghost)",
            "Furious S01E02 (2026)",
        ),
        (
            "The.Innocent.S01E01.SPANISH.720p.NF.WEBRip.x264-GalaxyTV",
            "The Innocent S01E01",
        ),
        ("Show.Name.S02E03-E05.1080p.WEB-DL", "Show Name S02E03-E05"),
        ("Show.S01E03.Back.to.1985.1080p", "Show S01E03"),
        # episodes told apart by something else keep it
        ("[SubsPlease] Frieren - 12 (1080p) [ABCD1234]", "Frieren - 12"),
        (
            "The.Daily.Show.2024.01.15.Guest.Name.1080p.WEB.h264-GRP",
            "The Daily Show 2024 01 15 Guest Name",
        ),
        # dots only separate words when nothing else does
        ("Mr. Robot S01E01 720p", "Mr. Robot S01E01"),
        (
            "Zizek, The_Pervert_s_Guide_To_Ideology",
            "Zizek, The Pervert s Guide To Ideology",
        ),
        # a lowercase release gets capitals
        (
            "squid.game.s03e04.1080p.web.h264-successfulcrab[EZTVx.to]",
            "Squid Game S03E04",
        ),
        # nothing to tidy, or nothing but tags
        ("OutofNothingKickstarter", "OutofNothingKickstarter"),
        ("1080p", "1080p"),
    ],
)
def test_humanize_title(stem: str, title: str):
    assert humanize_title(stem) == title
