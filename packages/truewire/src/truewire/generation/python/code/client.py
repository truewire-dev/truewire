from dataclasses import dataclass

from truewire.generation.util import indent
from truewire.generation.python.util import escape_docstring
from . import Function, Docstring, HttpRequest

@dataclass(kw_only=True)
class Client:
  """Endpoint class generator: one class wrapping one request method."""
  class_name: str
  mixin: str | None = None
  docs: str | None = None
  """Class docstring, which `python.md` requires on every generated class."""
  header: Function
  docstring: Docstring
  request: HttpRequest
  call_code: str = 'r = await self.request'
  return_code: str = 'return r.json()'
  tab: str = '  '

  def class_header(self) -> str:
    """Render the class statement, followed by its docstring when there is one."""
    out = f'class {self.class_name}'
    if self.mixin:
      out += f'({self.mixin})'
    out += ':'
    if self.docs:
      out += f'\n{self.tab}"""{escape_docstring(self.docs)}"""'
    return out

  def method_code(self) -> str:
    """Render the request method: header, docstring, request construction, return."""
    out = self.header.code()
    if (docstring := self.docstring.code()):
      out += '\n' + indent(docstring, self.tab)
    if (params := self.request.params_declaration()):
      out += '\n' + indent(params, self.tab)
    if (headers := self.request.headers_declaration()):
      out += '\n' + indent(headers, self.tab)
    out += '\n' + indent(self.request.request_call(self.call_code), self.tab)
    out += '\n' + indent(self.return_code, self.tab)
    return out

  def code(self) -> str:
    """Render the whole endpoint class."""
    out = self.class_header()
    out += '\n' + indent(self.method_code(), self.tab)
    return out