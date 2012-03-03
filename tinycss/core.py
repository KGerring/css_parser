# coding: utf8
"""
    tinycss.parser
    --------------

    Simple recursive-descent parser for the CSS core syntax:
    http://www.w3.org/TR/CSS21/syndata.html#tokenization

    :copyright: (c) 2010 by Simon Sapin.
    :license: BSD, see LICENSE for more details.
"""

from __future__ import unicode_literals
from itertools import chain
import functools
import sys
import re

from .tokenizer import tokenize_grouped, ContainerToken


#  stylesheet  : [ CDO | CDC | S | statement ]*;
#  statement   : ruleset | at-rule;
#  at-rule     : ATKEYWORD S* any* [ block | ';' S* ];
#  block       : '{' S* [ any | block | ATKEYWORD S* | ';' S* ]* '}' S*;
#  ruleset     : selector? '{' S* declaration? [ ';' S* declaration? ]* '}' S*;
#  selector    : any+;
#  declaration : property S* ':' S* value;
#  property    : IDENT;
#  value       : [ any | block | ATKEYWORD S* ]+;
#  any         : [ IDENT | NUMBER | PERCENTAGE | DIMENSION | STRING
#                | DELIM | URI | HASH | UNICODE-RANGE | INCLUDES
#                | DASHMATCH | ':' | FUNCTION S* [any|unused]* ')'
#                | '(' S* [any|unused]* ')' | '[' S* [any|unused]* ']'
#                ] S*;
#  unused      : block | ATKEYWORD S* | ';' S* | CDO S* | CDC S*;


class Stylesheet(object):
    """
    A parsed CSS stylesheet.

    .. attribute:: rules
        a mixed list of :class:`AtRule` and :class:`RuleSets` as returned by
        :func:`parse_at_rule` and :func:`parse_ruleset`, in source order

    .. attribute:: errors
        a list of :class:`ParseError`

    """
    def __init__(self, rules, errors):
        self.rules = rules
        self.errors = errors

    def __repr__(self):  # pragma: no cover
        return '<{0.__class__.__name__} {1} rules {2} errors>'.format(
            self, len(self.rules), len(self.errors))

    def pretty(self):  # pragma: no cover
        """Return an indented string representation for debugging"""
        lines = [rule.pretty() for rule in self.rules] + [
                 e.message for e in self.errors]
        return '\n'.join(lines)


class AtRule(object):
    """
    An unparsed at-rule.

    .. attribute:: at_keyword
        The normalized (lower-case) at-keyword as a string. eg. '@page'

    .. attribute:: head
        The "head" of the at-rule until ';' or '{': a list of tokens
        (:class:`Token` or :class:`ContainerToken`)

    .. attribute:: body
        A block as a '{' :class:`ContainerToken`, or ``None`` if the at-rule
        ends with ';'

    The head was validated against the core grammar but **not** the body,
    as the body might contain declarations. In case of an error in a
    declaration, parsing should continue from the next declaration.
    The whole rule should not be ignored as it would be for an error
    in the head.

    You are expected to parse and validate these at-rules yourself.

    """
    def __init__(self, at_keyword, head, body, line, column):
        self.at_keyword = at_keyword
        self.head = head
        self.body = body
        self.line = line
        self.column  = column

    def __repr__(self):  # pragma: no cover
        return ('<{0.__class__.__name__} {0.line}:{0.column} {0.at_keyword}>'
                .format(self))

    def pretty(self):  # pragma: no cover
        """Return an indented string representation for debugging"""
        lines = [self.at_keyword]
        for token in self.head:
            for line in token.pretty().splitlines():
                lines.append('    ' + line)
        if self.body:
            lines.append(self.body.pretty())
        else:
            lines.append(';')
        return '\n'.join(lines)


class RuleSet(object):
    """A ruleset.

    .. attribute:: at_keyword
        Always ``None``. Helps to tell rulesets apart from at-rules.

    .. attribute:: selector
        A (possibly empty) :class:`ContainerToken`

    .. attribute:: declarations
        The list of :class:`Declaration` as returned by
        :func:`parse_declaration_list`, in source order.

    """
    def __init__(self, selector, declarations, line, column):
        self.selector = selector
        self.declarations = declarations
        self.line = line
        self.column  = column

    def __repr__(self):  # pragma: no cover
        return ('<{0.__class__.__name__} at {0.line}:{0.column}'
                ' {0.selector.as_css}>'.format(self))

    def pretty(self):  # pragma: no cover
        """Return an indented string representation for debugging"""
        lines = [self.selector.pretty(), '{']
        for declaration in self.declarations:
            for line in declaration.pretty().splitlines():
                lines.append('    ' + line)
        lines.append('}')
        return '\n'.join(lines)

    at_keyword = None


class Declaration(object):
    """A property declaration.

    .. attribute:: name
        The property name as a normalized (lower-case) string.

    .. attribute:: values
        The property value: a list of tokens as returned by
        :func:`parse_value`.

    """
    def __init__(self, name, value, line, column):
        self.name = name
        self.value = value
        self.line = line
        self.column  = column

    def __repr__(self):  # pragma: no cover
        return ('<{0.__class__.__name__} {0.line}:{0.column}'
                ' {0.name}: {0.value.as_css}>'.format(self))

    def pretty(self):  # pragma: no cover
        """Return an indented string representation for debugging"""
        lines = [self.name + ':']
        for token in self.value.content:
            for line in token.pretty().splitlines():
                lines.append('    ' + line)
        return '\n'.join(lines)


class ParseError(ValueError):
    """A recoverable parsing error."""
    def __init__(self, subject, reason):
        self.subject = subject
        self.reason = reason
        self.msg = self.message = (
            'Parse error at {0.subject.line}:{0.subject.column}, {0.reason}'
            .format(self))
        super(ParseError, self).__init__(self.message)

    def __repr__(self):  # pragma: no cover
        return ('<{0.__class__.__name__}: {0.message}>'.format(self))


class CoreParser(object):
    """
    Currently the parser holds no state. It is only a class to allow
    subclassing and overriding its methods.

    """

    # User API:

    def parse_stylesheet(self, css_source):
        """Parse a stylesheet.

        :param css_source:
            A CSS stylesheet as an unicode string.
        :return:
            A :class:`Stylesheet`.

        """
        return Stylesheet(*self.parse_rules(tokenize_grouped(css_source)))


    def parse_style_attr(self, css_source):
        """Parse a "style" attribute (eg. of an HTML element).

        :param css_source:
            The attribute value, as an unicode string.
        :return:
            A tuple of the list of valid :class`Declaration` and a list
            of :class:`ParseError`.
        """
        return self.parse_declaration_list(tokenize_grouped(css_source))

    # API for subclasses:

    def parse_rules(self, tokens):
        """Parse a stylesheet, ie. a sequence of rulesets and at-rules.

        :param tokens:
            An iterable of tokens.
        :return:
            A tuple of a list of rules and a list of :class:`ParseError`.

        """
        parse_at_rule_methods = []
        for class_ in type(self).mro():
            method = vars(class_).get('parse_at_rule')
            if method:
                parse_at_rule_methods.append(method)
        rules = []
        errors = []
        tokens = iter(tokens)
        for token in tokens:
            if token.type not in ('S', 'CDO', 'CDC'):
                try:
                    if token.type == 'ATKEYWORD':
                        rule = self.read_at_rule(token, tokens)
                        for parse_at_rule in parse_at_rule_methods:
                            # These are unbound methods: they need
                            # to be passed self explicitly.
                            handled = parse_at_rule(self, rule, rules, errors)
                            if handled:
                                break
                        else:
                            errors.append(ParseError(
                                rule, 'unknown at-rule: ' + rule.at_keyword))
                    else:
                        rule, rule_errors = self.parse_ruleset(token, tokens)
                        rules.append(rule)
                        errors.extend(rule_errors)
                except ParseError as e:
                    errors.append(e)
                    # Skip the entire rule
        return rules, errors


    def parse_at_rule(self, rule, stylesheet_rules, errors):
        """Parse an at-rule.

        The parser will call this methods on each of the classes in its MRO
        (in order) so these methods never need to use ``super()``.

        If any method returns ``True``, it indicates that it has handled
        the at-rule (appended something to ``stylesheet_rules`` or to
        ``errors``). The parser stops there for this at-rule. Otherwise,
        it continues with the next class in the MRO.

        A method can also raise a :class:`ParseError`. The error is added
        to the list, and the rule is considered handled.

        At-rules that are not handled at all are ignored with an
        "Unknown at-rule" error.

        In :class:`CoreParser`, this method only handles @charset rules.
        (@import, @media and @page are in :class`CSS21Parser`.)

        :param rule:
            An unparsed :class:`AtRule`.
        :param stylesheet_rules:
            The list of at-rules and rulesets that have been parsed so far
            in this stylesheet. This method can append to this list
            (to add a valid, parsed at-rule) or inspect it to decide if
            the rule is valid. (For example, @import rules are only allowed
            before anything but a @charset rule.)
        :return:
            Whether the at-rule was handled. (bool)

        """
        if rule.at_keyword == '@charset':
            # (1, 1) assumes that the byte order mark (BOM), if any,
            # was removed when decoding bytes to Unicode.
            if (rule.line, rule.column) == (1, 1):
                if not (len(rule.head) == 1 and rule.head[0].type == 'STRING'
                        and rule.head[0].as_css[0] == '"' and not rule.body):
                    raise ParseError(rule, 'invalid @charset rule')
            else:
                raise ParseError(rule,
                    '@charset rule not at the beginning of the stylesheet')
            return True
        return False


    def read_at_rule(self, at_keyword_token, tokens):
        """Read an at-rule.

        :param at_keyword_token:
            The ATKEYWORD token that starts this at-rule
            You may have read it already to distinguish the rule
            from a ruleset.
        :param tokens:
            An iterator of subsequent tokens. Will be consumed just enough
            for one at-rule.
        :return:
            An unparsed :class:`AtRule`
        :raises:
            :class:`ParseError` if the head is invalid for the core grammar.
            The body is **not** validated. See :class:`AtRule`.

        """
        # CSS syntax is case-insensitive
        at_keyword = at_keyword_token.value.lower()
        head = []
        # For the ParseError in case `tokens` is empty:
        token = at_keyword_token
        for token in tokens:
            if token.type in '{;':
                for head_token in head:
                    self.validate_any(head_token, 'at-rule head')
                if token.type == '{':
                    body = token
                else:
                    body = None
                return AtRule(at_keyword, head, body,
                              at_keyword_token.line, at_keyword_token.column)
            # Ignore white space just after the at-keyword,
            # but keep it afterwards
            elif head or token.type != 'S':
                head.append(token)
        raise ParseError(token, 'incomplete at-rule')


    def parse_ruleset(self, first_token, tokens):
        """Parse a ruleset: a selector followed by declaration block.

        :param first_token:
            The first token of the ruleset (probably of the selector).
            You may have read it already to distinguish the rule
            from an at-rule.
        :param tokens:
            an iterator of subsequent tokens. Will be consumed just enough
            for one ruleset.
        :return:
            a tuple of a :class:`RuleSet` and an error list.
            The errors are recovered :class:`ParseError` in declarations.
            (Parsing continues from the next declaration on such errors.)
        :raises:
            :class:`ParseError` if the selector is invalid for the
            core grammar.
            Note a that a selector can be valid for the core grammar but
            not for CSS 2.1 or another level.

        """
        selector_parts = []
        for token in chain([first_token], tokens):
            if token.type == '{':
                # Parse/validate once we’ve read the whole rule
                for selector_token in selector_parts:
                    self.validate_any(selector_token, 'selector')
                start = selector_parts[0] if selector_parts else token
                selector = ContainerToken(
                    'SELECTOR', '', '', selector_parts,
                    start.line, start.column)
                declarations, errors = self.parse_declaration_list(
                    token.content)
                ruleset = RuleSet(selector, declarations,
                                  first_token.line, first_token.column)
                return ruleset, errors
            else:
                selector_parts.append(token)
        raise ParseError(token, 'no declaration block found for ruleset')


    def parse_declaration_list(self, tokens):
        """Parse a ';' separated declaration list.

        If you have a block that contains declarations but not only
        (like ``@page`` in CSS 3 Paged Media), you need to extract them
        yourself and use :func:`parse_declaration` directly.

        :param tokens:
            an iterable of tokens. Should stop at (before) the end
            of the block, as marked by a '}'.
        :return:
            a tuple of the list of valid :class`Declaration` and a list
            of :class:`ParseError`

        """
        # split at ';'
        parts = []
        this_part = []
        for token in tokens:
            type_ = token.type
            if type_ == ';':
                if this_part:
                    parts.append(this_part)
                this_part = []
            # skip white space at the start
            elif this_part or type_ != 'S':
                this_part.append(token)
        if this_part:
            parts.append(this_part)

        declarations = []
        errors = []
        for part in parts:
            try:
                declarations.append(self.parse_declaration(part))
            except ParseError as e:
                errors.append(e)
                # Skip the entire declaration
        return declarations, errors


    def parse_declaration(self, tokens):
        """Parse a single declaration.

        :param tokens:
            an iterable of at least one token. Should stop at (before)
            the end of the declaration, as marked by a ';' or '}'.
            Empty declarations (ie. consecutive ';' with only white space
            in-between) should skipped and not passed to this function.
        :returns:
            a :class:`Declaration`
        :raises:
            :class:`ParseError` if the tokens do not match the 'declaration'
            production of the core grammar.

        """
        tokens = iter(tokens)

        name_token = next(tokens)  # assume there is at least one
        if name_token.type == 'IDENT':
            # CSS syntax is case-insensitive
            property_name = name_token.value.lower()
        else:
            raise ParseError(name_token,
                'expected a property name, got {0}'.format(name_token.type))

        for token in tokens:
            if token.type == ':':
                break
            elif token.type != 'S':
                raise ParseError(
                    token, "expected ':', got {0}".format(token.type))
        else:
            raise ParseError(token, "expected ':'")

        value = self.parse_value(tokens)
        if not value:
            raise ParseError(token, 'expected a property value')
        value = ContainerToken(
            'VALUES', '', '', value, value[0].line, value[0].column)
        return Declaration(
            property_name, value, name_token.line, name_token.column)


    def parse_value(self, tokens):
        """Parse a property value and return a list of tokens.

        :param tokens:
            an iterable of tokens
        :return:
            a list of tokens with white space removed at the start and end,
            but not in the middle.
        :raises:
            :class:`ParseError` if there is any invalid token for the 'value'
            production of the core grammar.

        """
        content = []
        for token in tokens:
            type_ = token.type
            # Skip white space at the start
            if content or type_ != 'S':
                if type_ == '{':
                    self.validate_block(token.content, 'property value')
                else:
                    self.validate_any(token, 'property value')
                content.append(token)

        # Remove white space at the end
        while content and content[-1].type == 'S':
            content.pop()
        return content


    def validate_block(self, tokens, context):
        """
        :raises:
            :class:`ParseError` if there is any invalid token for the 'block'
            production of the core grammar.
        :param tokens: an iterable of tokens
        :param context: a string for the 'unexpected in ...' message

        """
        for token in tokens:
            type_ = token.type
            if type_ == '{':
                self.validate_block(token.content, context)
            elif type_ not in (';', 'ATKEYWORD'):
                self.validate_any(token, context)


    def validate_any(self, token, context):
        """
        :raises:
            :class:`ParseError` if this is an invalid token for the
            'any' production of the core grammar.
        :param token: a single token
        :param context: a string for the 'unexpected in ...' message

        """
        type_ = token.type
        if type_ in ('FUNCTION', '(', '['):
            for token in token.content:
                self.validate_any(token, type_)
        elif type_ not in ('S', 'IDENT', 'DIMENSION', 'PERCENTAGE', 'NUMBER',
                           'URI', 'DELIM', 'STRING', 'HASH', ':',
                           'UNICODE-RANGE'):
            if type_ in ('}', ')', ']'):
                adjective = 'unmatched'
            else:
                adjective = 'unexpected'
            raise ParseError(token,
                '{0} {1} token in {2}'.format(adjective, type_, context))