import re
import vim
from datetime import datetime

from tasklib.task import Task, SerializingObject
import regexp
import viewport
import util


def convert_priority_from_tw_format(priority):
    return {None: None, 'L': 1, 'M': 2, 'H': 3}[priority]


def convert_priority_to_tw_format(priority):
    return {0: None, 1: 'L', 2: 'M', 3: 'H'}[priority]


class VimwikiTask(object):
    # Lists all data keys that are reflected in Vim representation
    buffer_keys = ('indent', 'description', 'uuid', 'completed_mark',
                   'completed', 'line_number', 'priority', 'due')

    def __init__(self, cache):
        """
        Constructs a Vimwiki task from line at given position at the buffer
        """
        self.cache = cache
        self.tw = cache.tw
        self._task = None
        self.data = dict(indent='', completed_mark=' ')
        self._buffer_data = None

    def __getitem__(self, key):
        return self.data.get(key)

    def __setitem__(self, key, value):
        self.data[key] = value

    @classmethod
    def from_current_line(cls, cache):
        line_number = util.get_current_line_number()
        return cls.from_line(cache, line_number)

    @classmethod
    def from_line(cls, cache, number):
        """
        Creates a Vimwiki object from given line in the buffer.
          - If line does not contain a Vimwiki task, returns None.
        """

        match = re.search(regexp.GENERIC_TASK, vim.current.buffer[number])

        if not match:
            return None

        self = cls(cache)

        self.data.update({
            'indent': match.group('space'),
            'description': match.group('text'),
            'uuid': match.group('uuid'),  # can be None for new tasks
            'completed_mark': match.group('completed'),
            'completed': match.group('completed') is 'X',
            'line_number': number,
            'priority': convert_priority_to_tw_format(
                len(match.group('priority') or [])) # This is either 0,1,2 or 3
            })

        # To get local time aware timestamp, we need to convert to from local datetime
        # to UTC time, since that is what tasklib (correctly) uses
        due = match.group('due')
        if due:
            # With strptime, we get a native datetime object
            try:
                parsed_due = datetime.strptime(due, regexp.DATETIME_FORMAT)
            except ValueError:
                try:
                    parsed_due = datetime.strptime(due, regexp.DATE_FORMAT)
                except ValueError:
                    vim.command('echom "Taskwiki: Invalid timestamp on line %s, '
                                'ignored."' % self['line_number'])

            # We need to interpret it as timezone aware object in user's timezone
            # This properly handles DST, timezone offset and everything
            self['due'] = SerializingObject(self.tw).datetime_normalizer(parsed_due)

        # After all line-data parsing, save the data in the buffer
        self._buffer_data = {key:self[key] for key in self.buffer_keys}

        # We need to track depedency set in a extra attribute, since
        # this may be a new task, and hence it need not to be saved yet.
        # We circumvent this problem by iteration order in the TaskCache
        self.add_dependencies = set()

        self.parent = self.find_parent_task()

        # Make parent task dependant on this task
        if self.parent:
            self.parent.add_dependencies |= set([self])

        # For new tasks, apply defaults from above viewport
        if not self['uuid']:
            self.apply_defaults()

        return self

    @classmethod
    def from_task(cls, cache, task):
        self = cls(cache)
        self._task = task
        self.update_from_task()

        return self

    @property
    def task(self):
        # Return the corresponding task if alrady set
        if self._task is not None:
            return self._task

        # Else try to load it or create a new one
        if self['uuid']:
            try:
                self._task = self.cache[self['uuid']]
            except Task.DoesNotExist:
                self._task = Task(self.tw)
                # If task cannot be loaded, we need to remove the UUID
                vim.command('echom "UUID not found: %s,'
                            'will be replaced if saved"' % self['uuid'])
                self['uuid'] = None
        else:
            self._task = Task(self.tw)

        return self._task

    @task.setter
    def task(self, task):
        # Make sure we're updating by a correct task
        if task['uuid'] != self['uuid']:
            raise ValueError("Task '%s' with '%s' cannot be updated by "
                             "task with uuid '%s'."
                             % (self['description'],
                                self['uuid'],
                                task['uuid']))

        self._task = task

    @property
    def priority_from_tw_format(self):
        return convert_priority_from_tw_format(self.task['priority'])

    @property
    def priority_to_tw_format(self):
        return convert_priority_to_tw_format(self['priority'])

    @property
    def tainted(self):
        return any([
            self.task['description'] != self['description'],
            self.task['priority'] != self['priority'],
            self.task['due'] != self['due'],
            self.task['project'] != self['project'],
        ])

    def save_to_tw(self):

        # Push the values to the Task only if the Vimwiki representation
        # somehow differs
        # TODO: Check more than description
        if self.tainted or not self['uuid']:
            self.task['description'] = self['description']
            self.task['priority'] = self['priority']
            self.task['due'] = self['due']
            # TODO: this does not solve the issue of changed or removed deps (moved task)
            self.task['depends'] |= set(s.task for s in self.add_dependencies
                                        if not s.task.completed)
            # Since project is not updated in vimwiki on change per task, push to TW only
            # if defined
            if self['project']:
                self.task['project'] = self['project']
            self.task.save()

            # If we saved the task, we need to update. Hooks may have chaned data.
            self.update_from_task()

        # Load the UUID
        if not self['uuid']:
            self['uuid'] = self.task['uuid']

        # Mark task as done. This works fine with already completed tasks.
        if self['completed'] and (self.task.pending or self.task.waiting):
            self.task.done()

    def update_from_task(self):
        if not self.task.saved:
            return

        self.data.update({
            'description': self.task['description'],
            'priority': self.priority_from_tw_format,
            'completed': (self.task['status'] == u'completed'),
            'due': self.task['due'],
            'project': self.task['project'],
            'uuid': self.task['uuid'],
            })

    def update_in_buffer(self):
        # Look if any of the data that show up in Vim has changed
        buffer_data = {key:self[key] for key in self.buffer_keys}
        if self._buffer_data != buffer_data:
            # If so, update the line in vim and saved buffer data
            vim.current.buffer[self['line_number']] = str(self)
            self._buffer_data = buffer_data

    def __str__(self):
        return ''.join([
            self['indent'],
            '* [',
            'X' if self['completed'] else self['completed_mark'],
            '] ',
            self['description'] if self['description'] else 'TEXT MISSING?',
            ' ' + '!' * self.priority_from_tw_format if self['priority'] else '',
            ' ' + self['due'].strftime(regexp.DATETIME_FORMAT) if self['due'] else '',
            '  #' + self['uuid'] if self['uuid'] else '',
        ])

    def find_parent_task(self):
        for i in reversed(range(0, self['line_number'])):
            # The from_line constructor returns None if line doesn't match a task
            task = self.cache[i]
            if task and len(task['indent']) < len(self['indent']):
                return task

    def apply_defaults(self):
        for i in reversed(range(0, self['line_number'])):
            port = viewport.ViewPort.from_line(i, self.cache)
            if port and port.defaults:
                self.data.update(port.defaults)
                break
            # Break on line which does not look like a task
            elif not vim.current.buffer[i].strip().startswith("*"):
                break
