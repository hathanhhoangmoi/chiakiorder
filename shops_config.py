import re

SHOPS = [
    ("https://chiaki.vn/stz88clwvl-st2732", "Min Duty", "STZ88CLWVL"),
    ("https://chiaki.vn/gian-hang-st4299", "HADES Shop", "STPU7XXLRO"),
    ("https://chiaki.vn/gian-hang-st3522", "ShipnhanhStore", "ST8RBD8N7P"),
    ("https://chiaki.vn/stk635opng-st4337", "TH Cosmetic", "STK635OPNG"),
    ("https://chiaki.vn/stf4cahsa3-st3684", "Hoya Life", "STF4CAHSA3"),
    ("https://chiaki.vn/stioaqzbw3-st3540", "Rongcon", "ST0FB63UNK"),
    ("https://chiaki.vn/stioaqzbw3-st3783", "Đẹp & Khoẻ 365", "ST52DPRAAQ"),
    ("https://chiaki.vn/stioaqzbw3-st1489", "Beauty & Healthy", "STYL4YS4G4"),
    ("https://chiaki.vn/stioaqzbw3-st4036", "Kitty House", "STXSHR33MD"),
    ("https://chiaki.vn/stioaqzbw3-st635", "Trang Perfume", "STMVBWFASC"),
    ("https://chiaki.vn/stioaqzbw3-st1498", "Kho Dược TPCN", "STIOAQZBW3"),
    ("https://chiaki.vn/gian-hang-st5090", "Mason House Store", "ST6UI8IKUZ"),
    ("https://chiaki.vn/gian-hang-st5091", "Gia Phương Shop", "ST5V3Z9EZ0"),
    ("https://chiaki.vn/gian-hang-st4339", "PINASAGO Pin Sài Gòn", "STBCBL0Q2J"),
    ("https://chiaki.vn/gian-hang-st4961", "Kalos Việt Nam", "ST7A4S1NMX"),
    ("https://chiaki.vn/gian-hang-st4872", "Green House", "STFXBK1K2R"),
    ("https://chiaki.vn/gian-hang-st2292", "Thế Giới Hàng Auth 88", "STD3YL1TSI"),
    ("https://chiaki.vn/gian-hang-st1164", "O2 PHARMACY", "ST62778NKR"),
    ("https://chiaki.vn/gian-hang-st3612", "NGUYENKIM", "STYKS36NRV"),
    ("https://chiaki.vn/gian-hang-st5092", "Thế Giới Son", "STICBF43TU"),
    ("https://chiaki.vn/gian-hang-st1600", "ChoychoyStore", "ST8TO6BBH6"),
    ("https://chiaki.vn/gian-hang-st2423", "Peony Cosmetics", "STMDTS134P"),
    ("https://chiaki.vn/gian-hang-st4965", "nhathuocsuckhoe2", "STD14EBRRV"),
    ("https://chiaki.vn/gian-hang-st4365", "Nana Beauty & More", "ST8IXXEWFP"),
    ("https://chiaki.vn/gian-hang-st1729", "MHDMART", "ST0TQGJL1P"),
    ("https://chiaki.vn/gian-hang-st4360", "SnapshopVN", "STC2LLEPG1"),
    ("https://chiaki.vn/gian-hang-st5094", "Shop Snap TPHCM", "STC211TCK1"),
    ("https://chiaki.vn/gian-hang-st1602", "Winny Shop", "ST7VUHBQ2S"),
    ("https://chiaki.vn/gian-hang-st1105", "MjuMju", "STX77TCAQJ"),
    ("https://chiaki.vn/gian-hang-st4294", "Life Healthy", "ST2V1HXT04"),
    ("https://chiaki.vn/gian-hang-st3009", "QuangNgoc1976", "STHT5W6Q1I"),
    ("https://chiaki.vn/gian-hang-st1414", "Dược Phẩm Tâm An", "STUK6KK02F"),
    ("https://chiaki.vn/gian-hang-st2573", "MiMi Beauty", "STODIB4140"),
    ("https://chiaki.vn/gian-hang-st2225", "T&T Japan shop", "ST0RHGMRT1"),
    ("https://chiaki.vn/gian-hang-st3047", "GREENBOX", "STF86PB7X5"),
    ("https://chiaki.vn/gian-hang-st3864", "Hàng ngoại giá tốt", "STJIBLAFNM"),
    ("https://chiaki.vn/gian-hang-st3852", "Baby Grow Shop", "STIT1BWG3G"),
    ("https://chiaki.vn/gian-hang-st1273", "NHÀ THUỐC MINH TÂM", "STTYOFL1AL"),
    ("https://chiaki.vn/gian-hang-st3300", "Mayya Hàng Nội Địa Nhật", "STAIXRWW85"),
    ("https://chiaki.vn/gian-hang-st1258", "Shop Adam", "STAFZIGJ52"),
    ("https://chiaki.vn/gian-hang-st3337", "Shop Sài Gòn", "STNNQQ9VM4"),
    ("https://chiaki.vn/gian-hang-st4342", "Sản Phẩm Hỗ Trợ", "STHK74IIOX"),
    ("https://chiaki.vn/gian-hang-st1015", "Green Healthy & Beauty", "STZM5MDWT0"),
    ("https://chiaki.vn/gian-hang-st3218", "Bảo Lâm Anh", "STPNJGJTR5"),
    ("https://chiaki.vn/gian-hang-st2557", "XUAN MINH PHARMACY", "STSV6MW0BI"),
    ("https://chiaki.vn/gian-hang-st2025", "Cali-Goods", "STKT8L8BFD"),
]
def extract_id(url: str) -> str | None:
    m = re.search(r'-st(\d+)', url, re.IGNORECASE)
    return m.group(1) if m else None
SHOP_NAME_MAP = {code: name for _, name, code in SHOPS}
BLOCKED_SHOPS = {
    "0001",
}
SHOP_ID_NAME_MAP = {
    extract_id(url): name
    for url, name, code in SHOPS
    if extract_id(url)
}

def get_shops_map() -> dict:
    result = {}
    for url, name, code in SHOPS:
        sid = extract_id(url)
        if sid:
            result[sid] = (url, name)
    return result
