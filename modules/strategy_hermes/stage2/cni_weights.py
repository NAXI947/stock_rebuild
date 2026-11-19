"""Verified CNI identities and fixed-size references for existing weight data."""
from decimal import Decimal

VERSION = "cni-index-reference-v1"
REFERENCES = {
    "399006": (100, "https://www.cnindex.com.cn/docs/gz_399006.pdf"),
    "399441": (30, "https://www.cnindex.com.cn/docs/gz_399441.pdf"),
}
ALIASES = {f"{code}.{suffix}": code for code in REFERENCES for suffix in ("SZ", "CNI")}


def provider_code(index):
    return ALIASES.get(index)


def identity(index):
    code = provider_code(index)
    if code is None:
        return None
    expected, evidence = REFERENCES[code]
    return dict(provider="CNI", provider_index_code=code, index_code=index,
                data_index_code=f"{code}.SZ", expected_count=expected, source_ref=evidence,
                reference_version=VERSION)


def apply_reference(basis, index):
    """Validate one Tushare date against the reviewed CNI sample count."""
    reference = identity(index)
    if reference is None:
        return
    basis.update(index_identity=reference)
    total = sum((Decimal(row["weight"]) for row in basis["rows"]), Decimal(0))
    complete = len(basis["rows"]) == reference["expected_count"] and abs(total - 1) <= Decimal(".005")
    basis.update(complete=complete, completeness_basis="CNI_DOCUMENTED_COUNT" if complete else "CNI_PARTIAL_SET")
