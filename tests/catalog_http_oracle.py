"""Fixed HTTP bytes shared by both native database integrations."""

EXPECTED_OPERATION_ORDER = (
    "discovery_first_page",
    "discovery_cursor_page",
    "nonempty_search_first_page",
    "nonempty_search_cursor_page",
    "facet_language_first_page",
    "facet_subject_first_page",
    "facet_contributor_first_page",
)
SMOKE_EXPECTED_BODY_SHA256 = {
    "discovery_first_page": (
        "51bddb85a66a7d2bef63867a073873fa8190ba5d484f7715a9e4220c218bea4e"
    ),
    "discovery_cursor_page": (
        "0bc3f6007a5c6c87c95d66eec1e264e09562d7c0e226f5159c55ab166777d608"
    ),
    "nonempty_search_first_page": (
        "d117f03ac54cd357b35cc6aed5b425ce5a3541cdfe58ecd5f8acb9bdeaeadffd"
    ),
    "nonempty_search_cursor_page": (
        "936581cd74839c5b274e757d27aaf61903c0b36bc101507fb668af2e17bbeba4"
    ),
    "facet_language_first_page": (
        "2eaa4e17580c5de00a02a346d9456e654959b89ef5f51e89928aa7576590f4d9"
    ),
    "facet_subject_first_page": (
        "75255856d4d1e5a4d418b4b2dfff0fcad31e34bc10ca47d0d2504645417601f1"
    ),
    "facet_contributor_first_page": (
        "09ed8dc7e16b039b6166e187ca245923b3e037fcf0662d8688a7395526951181"
    ),
}
