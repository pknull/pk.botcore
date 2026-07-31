import logging

from pk_botcore.peers import (
    linkify_peer_mentions,
    parse_peer_bots,
)


def test_parse_peer_bots_accepts_valid_pairs_and_normalizes_aliases():
    assert parse_peer_bots(" Asha : 123 , ZALGO:456 ") == {
        "asha": 123,
        "zalgo": 456,
    }


def test_parse_peer_bots_skips_malformed_entries_and_keeps_valid_ones(caplog):
    with caplog.at_level(logging.WARNING, logger="pk_botcore.peers"):
        peers = parse_peer_bots("asha:123,missing-id,nope:not-a-number,:456,zalgo:789")

    assert peers == {"asha": 123, "zalgo": 789}
    assert caplog.text.count("Skipping malformed PEER_BOTS entry") == 3


def test_parse_peer_bots_empty_values_are_inert():
    assert parse_peer_bots(None) == {}
    assert parse_peer_bots("") == {}
    assert parse_peer_bots("  ") == {}


def test_linkify_peer_mentions_is_explicit_case_insensitive_and_bounded():
    peers = {"asha": 123}

    assert linkify_peer_mentions("@asha", peers) == "<@123>"
    assert linkify_peer_mentions("@AsHa", peers) == "<@123>"
    assert linkify_peer_mentions("@ashamed", peers) == "@ashamed"
    assert linkify_peer_mentions("bare asha", peers) == "bare asha"


def test_linkify_peer_mentions_handles_multiple_aliases_and_punctuation():
    peers = {"asha": 123, "zalgo": 456}

    assert linkify_peer_mentions("@asha, ask (@ZALGO).", peers) == (
        "<@123>, ask (<@456>)."
    )


def test_linkify_peer_mentions_preserves_existing_tokens_and_empty_inputs():
    peers = {"123": 999}

    assert linkify_peer_mentions("Already <@123>", peers) == "Already <@123>"
    assert linkify_peer_mentions("No peers @asha", {}) == "No peers @asha"
    assert linkify_peer_mentions("", {"asha": 123}) == ""
