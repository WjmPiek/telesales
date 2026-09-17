import re


def display_brand(value):
    if not isinstance(value,str):return value
    text=re.sub(r"(?<![/\w])telesales\b(?![/_])", "Insurance Sales", value, flags=re.I)
    text=re.sub(r"\bMartins Funerals\b", "Martin's Funerals", text, flags=re.I)
    return type(value)(text)
