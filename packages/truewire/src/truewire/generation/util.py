def capitalize(w: str) -> str:
  return w[0].upper() + w[1:]

def pascal_case(word: str) -> str:
  from caseconverter import pascalcase
  return pascalcase(word)

def snake_case(word: str) -> str:
  from caseconverter import snakecase
  return snakecase(word)

def indent(text: str, tab: str = '  ') -> str:
  """Indent every non-blank line, leaving blank lines empty.

  Indenting a blank line would leave trailing whitespace on it, which every generated
  docstring and multi-line block would then carry.
  """
  return '\n'.join(tab + line if line.strip() else '' for line in text.split('\n'))
