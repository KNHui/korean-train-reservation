"""Project-local keyboard navigation for inquirer prompts."""

from copy import copy

from inquirer import errors, events, themes
from inquirer.render.console import ConsoleRender
from readchar import key


class PreviousStep(Exception):
    def __init__(self, value):
        self.value = value


class NavigationRender(ConsoleRender):
    def __init__(self, *args, allow_back=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_back = allow_back

    def _handle_navigation(self, render, pressed):
        kind = render.question.kind
        if self.allow_back and pressed == key.ESC:
            if kind in {"text", "password", "path"}:
                value = render.current
            elif kind == "list":
                choice = render.question.choices[render.current]
                value = getattr(choice, "value", choice)
            elif kind == "checkbox":
                choices = render.question.choices
                value = [getattr(choices[i], "value", choices[i]) for i in render.selection]
            else:
                value = render.question.default
            self._go_to_end(render)
            self._previous_error = None
            raise PreviousStep(value)

        if kind in {"text", "password", "path"} and pressed in {key.HOME, key.END}:
            render.cursor_offset = len(render.current) if pressed == key.HOME else 0
            render._autocomplete_state = None
            return True

        if kind in {"list", "checkbox"} and pressed in {key.UP, key.DOWN, key.HOME, key.END}:
            count = len(render.question.choices)
            if count:
                if pressed == key.HOME:
                    render.current = 0
                elif pressed == key.END:
                    render.current = count - 1
                else:
                    render.current = (render.current + (1 if pressed == key.DOWN else -1)) % count
            return True
        return False

    def _process_input(self, render):
        # Keep inquirer's validation behavior while handling navigation first.
        try:
            event = self._event_gen.next()
            if isinstance(event, events.KeyPressed):
                if not self._handle_navigation(render, event.value):
                    render.process_input(event.value)
        except errors.ValidationError as exc:
            self._previous_error = exc.value
        except errors.EndOfInput as exc:
            try:
                render.question.validate(exc.selection)
                raise
            except errors.ValidationError as validation:
                self._previous_error = render.handle_validation_error(validation)


def prompt_steps(questions, render, answers=None, raise_keyboard_interrupt=False):
    """Revisit steps with both submitted answers and unfinished edits intact."""
    answers = dict(answers or {})
    drafts = dict(answers)
    index = 0
    try:
        while index < len(questions):
            question = copy(questions[index])
            if question.name in drafts:
                value = drafts[question.name]
                # Question.default otherwise replaces empty strings/False with
                # the original default, and formats braces in text values.
                question._default = lambda _, value=value: value
            try:
                value = render.render(question, answers)
            except PreviousStep as previous:
                drafts[question.name] = previous.value
                answers.pop(question.name, None)
                if index == 0:
                    return None
                index -= 1
            else:
                answers[question.name] = drafts[question.name] = value
                index += 1
        return answers
    except KeyboardInterrupt:
        if raise_keyboard_interrupt:
            raise
        print("\n입력을 취소했습니다.")
        return None


class NavigationInquirer:
    """Wrap only this app's prompts, without changing the installed package."""

    def __init__(self, source):
        self.source = source

    def __getattr__(self, name):
        return getattr(self.source, name)

    def prompt(self, questions, render=None, answers=None, theme=None,
               raise_keyboard_interrupt=False, allow_back=False):
        render = render or NavigationRender(theme=theme or themes.Default(), allow_back=allow_back)
        if allow_back:
            return prompt_steps(questions, render, answers, raise_keyboard_interrupt)
        return self.source.prompt(questions, render=render, answers=answers,
                                  raise_keyboard_interrupt=raise_keyboard_interrupt)

    def list_input(self, *args, **kwargs):
        kwargs.setdefault("render", NavigationRender())
        return self.source.list_input(*args, **kwargs)
