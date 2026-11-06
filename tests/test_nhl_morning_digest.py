#!/usr/bin/env python3
"""Checks for the morning digest rules. Run: python -m pytest tests/test_nhl_morning_digest.py"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import nhl_morning_digest as digest

TODAY = dt.date(2026, 11, 7)
NOW = dt.datetime(2026, 11, 7, 7, 0, tzinfo=dt.timezone.utc)


def game(day="2026-11-07", start="2026-11-08T01:00:00Z", away="CHI", home="CAR", venue="Lenovo Center",
         tz="US/Eastern", special=None):
    row = {"id": hash((day, start, away, home)), "gameType": 2, "gameDate": day, "startTimeUTC": start,
           "venue": {"default": venue}, "venueTimezone": tz,
           "awayTeam": {"abbrev": away}, "homeTeam": {"abbrev": home}}
    if special:
        row["specialEvent"] = {"name": {"default": special}}
    return row


def landing(pid=1, team="CAR", country="CAN", games=998, last="2026-11-05", **extra):
    return {
        "playerId": pid, "firstName": {"default": "Taylor"}, "lastName": {"default": "Hall"},
        "currentTeamAbbrev": team, "isActive": True, "birthCountry": country,
        "careerTotals": {"regularSeason": {"gamesPlayed": games, **extra}},
        "last5Games": [{"gameDate": last}],
    }


def feed_row(pid=1, team="CAR", stat="Games Played", target=1000, value=998, game_type=2):
    key = {"Games Played": "gamesPlayed", "Wins": "wins"}[stat]
    return {"playerId": pid, "teamAbbrev": team, "milestone": stat, "milestoneAmount": target,
            key: value, "gameTypeId": game_type, "playerFullName": "Taylor Hall"}


def records(rows, profile, watch=()):
    upcoming = digest.games_in_window([game()], NOW)
    return digest.collect_records(list(rows), list(watch), upcoming, {}, TODAY, get_landing=lambda pid: profile)


def test_two_games_of_one_series_give_one_alert():
    games = [
        game("2026-11-12", "2026-11-12T17:00:00Z", "CAR", "SEA", "Veikkaus Arena", "Europe/Helsinki", "2026 NHL Global Series"),
        game("2026-11-14", "2026-11-14T18:00:00Z", "SEA", "CAR", "Veikkaus Arena", "Europe/Helsinki", "2026 NHL Global Series"),
    ]
    events = digest.build_events(games)
    series = [e for e in events if e["kind"] == "матч за границей"]
    assert len(series) == 1
    assert len(series[0]["games"]) == 2
    assert "20:00 МСК" in series[0]["games"][0]
    assert digest.events_due(series, dt.date(2026, 11, 9))[0]["days"] == 3
    assert digest.events_due(series, dt.date(2026, 10, 29))[0]["days"] == 14
    assert not digest.events_due(series, dt.date(2026, 11, 8))


def test_series_in_two_countries_stay_separate():
    games = [
        game("2026-11-12", "2026-11-12T17:00:00Z", "CAR", "SEA", "Veikkaus Arena", "Europe/Helsinki", "2026 NHL Global Series"),
        game("2026-12-18", "2026-12-18T18:00:00Z", "CHI", "OTT", "PSD Bank Dome", "Europe/Berlin", "2026 NHL Global Series"),
    ]
    assert len(digest.build_events(games)) == 2


def test_outdoor_game_counts_on_moscow_date():
    games = [game("2026-12-31", "2026-12-31T23:00:00Z", "COL", "UTA", "Rice-Eccles Stadium", "America/Denver", "Discover NHL Winter Classic")]
    event = digest.build_events(games)[0]
    assert event["kind"] == "на открытом воздухе"
    assert event["date"] == dt.date(2027, 1, 1)


def test_big_slate_importance():
    teams = list(digest.TEAM_RU)

    def slate(day, count):
        return [game(day, f"{day}T23:00:00Z", teams[i * 2], teams[i * 2 + 1]) for i in range(count)]

    games = slate("2027-04-10", 16) + slate("2027-02-13", 15) + slate("2027-01-16", 14) + slate("2027-01-17", 13)
    by_day = {e["date"].isoformat(): e for e in digest.build_events(games)}
    assert by_day["2027-04-10"]["importance"] == "high"
    assert "все 32" in by_day["2027-04-10"]["place"]
    assert by_day["2027-02-13"]["importance"] == "medium"
    assert by_day["2027-01-16"]["importance"] == "low"
    assert "2027-01-17" not in by_day


def test_low_importance_never_alerts():
    events = [{"date": TODAY + dt.timedelta(days=3), "importance": "low"}]
    assert digest.events_due(events, TODAY) == []


def test_record_line_uses_players_page_number():
    found = records([feed_row(value=997)], landing(games=998))
    assert found[0]["current"] == 998 and found[0]["remaining"] == 2


def test_record_dropped_when_team_has_no_game():
    assert records([feed_row(team="SEA")], landing(team="SEA")) == []


def test_record_dropped_when_too_far_or_reached():
    assert records([feed_row(value=996)], landing(games=996)) == []
    assert records([feed_row(value=999)], landing(games=1000)) == []


def test_small_targets_only_for_russians():
    row = feed_row(target=500, value=498)
    assert records([row], landing(games=498)) == []
    assert records([row], landing(games=498, country="RUS"))[0]["russian"] is True


def test_player_out_for_a_month_is_dropped():
    assert records([feed_row()], landing(last="2026-06-14")) == []


def test_playoff_and_minutes_rows_ignored():
    assert records([feed_row(game_type=3)], landing()) == []
    assert digest.feed_candidates([{**feed_row(), "milestone": "Minutes Played", "toiMinutes": 9999}]) == []


def test_watch_list_alerts_on_its_own_threshold():
    profile = landing(games=10, points=996, country="RUS")
    item = {"player_id": 1, "stat": "points", "target": 1000, "alert_at": 5}
    assert records([], profile, [item])[0]["remaining"] == 4
    assert records([], profile, [{**item, "alert_at": 3}]) == []


def test_nothing_useful_means_no_message():
    assert digest.render(TODAY, [], []) is None


def test_render_reads_like_the_owner_asked():
    found = records([feed_row(stat="Wins", target=500, value=497)], landing(games=800, wins=497, country="RUS"))
    text = digest.render(TODAY, [], found)
    assert "до 500 побед осталось 3 (сейчас 497)" in text
    assert "Соперник: Чикаго, начало в 04:00 МСК" in text
