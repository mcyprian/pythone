#!/usr/bin/python
'''
From gdb 7 onwards, gdb's build can be configured --with-python, allowing gdb
to be extended with Python code e.g. for library-specific data visualizations,
such as for the C++ STL types.  Documentation on this API can be seen at:
http://sourceware.org/gdb/current/onlinedocs/gdb/Python-API.html


This python module deals with the case when the process being debugged (the
"inferior process" in gdb parlance) is itself python, or more specifically,
linked against libpython.  In this situation, almost every item of data is a
(PyObject*), and having the debugger merely print their addresses is not very
enlightening.

This module embeds knowledge about the implementation details of libpython so
that we can emit useful visualizations e.g. a string, a list, a dict, a frame
giving file/line information and the state of local variables

In particular, given a gdb.Value corresponding to a PyObject* in the inferior
process, we can generate a "proxy value" within the gdb process.  For example,
given a PyObject* in the inferior process that is in fact a PyListObject*
holding three PyObject* that turn out to be PyStringObject* instances, we can
generate a proxy value within the gdb process that is a list of strings:
  ["foo", "bar", "baz"]

We try to defer gdb.lookup_type() invocations for python types until as late as
possible: for a dynamically linked python binary, when the process starts in
the debugger, the libpython.so hasn't been dynamically loaded yet, so none of
the type names are known to the debugger

The module also extends gdb with some python-specific commands.
'''

import gdb

# Look up the gdb.Type for some standard types:
_type_char_ptr = gdb.lookup_type('char').pointer() # char*
_type_unsigned_char_ptr = gdb.lookup_type('unsigned char').pointer() # unsigned char*
_type_void_ptr = gdb.lookup_type('void').pointer() # void*
_type_size_t = gdb.lookup_type('size_t')

SIZEOF_VOID_P = _type_void_ptr.sizeof


Py_TPFLAGS_HEAPTYPE = (1L << 9)

Py_TPFLAGS_INT_SUBCLASS      = (1L << 23)
Py_TPFLAGS_LONG_SUBCLASS     = (1L << 24)
Py_TPFLAGS_LIST_SUBCLASS     = (1L << 25)
Py_TPFLAGS_TUPLE_SUBCLASS    = (1L << 26)
Py_TPFLAGS_STRING_SUBCLASS   = (1L << 27)
Py_TPFLAGS_UNICODE_SUBCLASS  = (1L << 28)
Py_TPFLAGS_DICT_SUBCLASS     = (1L << 29)
Py_TPFLAGS_BASE_EXC_SUBCLASS = (1L << 30)
Py_TPFLAGS_TYPE_SUBCLASS     = (1L << 31)


class NullPyObjectPtr(RuntimeError):
    pass


def safety_limit(val):
    # Given a integer value from the process being debugged, limit it to some
    # safety threshold so that arbitrary breakage within said process doesn't
    # break the gdb process too much (e.g. sizes of iterations, sizes of lists)
    return min(val, 100)


def safe_range(val):
    # As per range, but don't trust the value too much: cap it to a safety
    # threshold in case the data was corrupted
    return xrange(safety_limit(val))


class PyObjectPtr(object):
    """
    Class wrapping a gdb.Value that's a either a (PyObject*) within the
    inferior process, or some subclass pointer e.g. (PyStringObject*)

    There will be a subclass for every refined PyObject type that we care
    about.

    Note that at every stage the underlying pointer could be NULL, point
    to corrupt data, etc; this is the debugger, after all.
    """
    _typename = 'PyObject'

    def __init__(self, gdbval, cast_to=None):
        if cast_to:
                self._gdbval = gdbval.cast(cast_to)
        else:
            self._gdbval = gdbval

    def field(self, name):
        '''
        Get the gdb.Value for the given field within the PyObject, coping with
        some python 2 versus python 3 differences.

        Various libpython types are defined using the "PyObject_HEAD" and
        "PyObject_VAR_HEAD" macros.

        In Python 2, this these are defined so that "ob_type" and (for a var
        object) "ob_size" are fields of the type in question.

        In Python 3, this is defined as an embedded PyVarObject type thus:
           PyVarObject ob_base;
        so that the "ob_size" field is located insize the "ob_base" field, and
        the "ob_type" is most easily accessed by casting back to a (PyObject*).
        '''
        if self.is_null():
            raise NullPyObjectPtr(self)

        if name == 'ob_type':
            pyo_ptr = self._gdbval.cast(PyObjectPtr.get_gdb_type())
            return pyo_ptr.dereference()[name]

        if name == 'ob_size':
            try:
                # Python 2:
                return self._gdbval.dereference()[name]
            except RuntimeError:
                # Python 3:
                return self._gdbval.dereference()['ob_base'][name]

        # General case: look it up inside the object:
        return self._gdbval.dereference()[name]

    def type(self):
        return PyTypeObjectPtr(self.field('ob_type'))

    def is_null(self):
        return 0 == long(self._gdbval)

    def safe_tp_name(self):
        try:
            return self.type().field('tp_name').string()
        except NullPyObjectPtr:
            # NULL tp_name?
            return 'unknown'
        except RuntimeError:
            # Can't even read the object at all?
            return 'unknown'

    def proxyval(self, visited):
        '''
        Scrape a value from the inferior process, and try to represent it
        within the gdb process, whilst (hopefully) avoiding crashes when
        the remote data is corrupt.

        Derived classes will override this.

        For example, a PyIntObject* with ob_ival 42 in the inferior process
        should result in an int(42) in this process.

        visited: a set of all gdb.Value pyobject pointers already visited
        whilst generating this value (to guard against infinite recursion when
        visiting object graphs with loops).  Analogous to Py_ReprEnter and
        Py_ReprLeave
        '''

        class FakeRepr(object):
            """
            Class representing a non-descript PyObject* value in the inferior
            process for when we don't have a custom scraper, intended to have
            a sane repr().
            """

            def __init__(self, tp_name, address):
                self.tp_name = tp_name
                self.address = address

            def __repr__(self):
                # For the NULL pointer, we have no way of knowing a type, so
                # special-case it as per
                # http://bugs.python.org/issue8032#msg100882
                if self.address == 0:
                    return '0x0'
                return '<%s at remote 0x%x>' % (self.tp_name, self.address)

        return FakeRepr(self.safe_tp_name(),
                        long(self._gdbval))

    @classmethod
    def subclass_from_type(cls, t):
        '''
        Given a PyTypeObjectPtr instance wrapping a gdb.Value that's a
        (PyTypeObject*), determine the corresponding subclass of PyObjectPtr
        to use

        Ideally, we would look up the symbols for the global types, but that
        isn't working yet:
          (gdb) python print gdb.lookup_symbol('PyList_Type')[0].value
          Traceback (most recent call last):
            File "<string>", line 1, in <module>
          NotImplementedError: Symbol type not yet supported in Python scripts.
          Error while executing Python code.

        For now, we use tp_flags, after doing some string comparisons on the
        tp_name for some special-cases that don't seem to be visible through
        flags
        '''
        try:
            tp_name = t.field('tp_name').string()
            tp_flags = int(t.field('tp_flags'))
        except RuntimeError:
            # Handle any kind of error e.g. NULL ptrs by simply using the base
            # class
            return cls

        #print 'tp_flags = 0x%08x' % tp_flags
        #print 'tp_name = %r' % tp_name

        name_map = {'bool': PyBoolObjectPtr,
                    'classobj': PyClassObjectPtr,
                    'instance': PyInstanceObjectPtr,
                    'NoneType': PyNoneStructPtr,
                    'frame': PyFrameObjectPtr,
                    'set' : PySetObjectPtr,
                    'frozenset' : PySetObjectPtr,
                    }
        if tp_name in name_map:
            return name_map[tp_name]

        if tp_flags & Py_TPFLAGS_HEAPTYPE:
            return HeapTypeObjectPtr

        if tp_flags & Py_TPFLAGS_INT_SUBCLASS:
            return PyIntObjectPtr
        if tp_flags & Py_TPFLAGS_LONG_SUBCLASS:
            return PyLongObjectPtr
        if tp_flags & Py_TPFLAGS_LIST_SUBCLASS:
            return PyListObjectPtr
        if tp_flags & Py_TPFLAGS_TUPLE_SUBCLASS:
            return PyTupleObjectPtr
        if tp_flags & Py_TPFLAGS_STRING_SUBCLASS:
            return PyStringObjectPtr
        if tp_flags & Py_TPFLAGS_UNICODE_SUBCLASS:
            return PyUnicodeObjectPtr
        if tp_flags & Py_TPFLAGS_DICT_SUBCLASS:
            return PyDictObjectPtr
        if tp_flags & Py_TPFLAGS_BASE_EXC_SUBCLASS:
            return PyBaseExceptionObjectPtr
        #if tp_flags & Py_TPFLAGS_TYPE_SUBCLASS:
        #    return PyTypeObjectPtr

        # Use the base class:
        return cls

    @classmethod
    def from_pyobject_ptr(cls, gdbval):
        '''
        Try to locate the appropriate derived class dynamically, and cast
        the pointer accordingly.
        '''
        try:
            p = PyObjectPtr(gdbval)
            cls = cls.subclass_from_type(p.type())
            return cls(gdbval, cast_to=cls.get_gdb_type())
        except RuntimeError:
            # Handle any kind of error e.g. NULL ptrs by simply using the base
            # class
            pass
        return cls(gdbval)

    @classmethod
    def get_gdb_type(cls):
        return gdb.lookup_type(cls._typename).pointer()

    def as_address(self):
        return long(self._gdbval)


class ProxyAlreadyVisited(object):
    '''
    Placeholder proxy to use when protecting against infinite recursion due to
    loops in the object graph.

    Analogous to the values emitted by the users of Py_ReprEnter and Py_ReprLeave
    '''
    def __init__(self, rep):
        self._rep = rep
    
    def __repr__(self):
        return self._rep

class InstanceProxy(object):

    def __init__(self, cl_name, attrdict, address):
        self.cl_name = cl_name
        self.attrdict = attrdict
        self.address = address

    def __repr__(self):
        if isinstance(self.attrdict, dict):
            kwargs = ', '.join(["%s=%r" % (arg, val)
                                for arg, val in self.attrdict.iteritems()])
            return '<%s(%s) at remote 0x%x>' % (self.cl_name,
                                                kwargs, self.address)
        else:
            return '<%s at remote 0x%x>' % (self.cl_name,
                                            self.address)
        

def _PyObject_VAR_SIZE(typeobj, nitems):
    return ( ( typeobj.field('tp_basicsize') +
               nitems * typeobj.field('tp_itemsize') +
               (SIZEOF_VOID_P - 1)
             ) & ~(SIZEOF_VOID_P - 1)
           ).cast(_type_size_t)

class HeapTypeObjectPtr(PyObjectPtr):
    _typename = 'PyObject'

    def proxyval(self, visited):
        '''
        Support for new-style classes.

        Currently we just locate the dictionary using a transliteration to
        python of _PyObject_GetDictPtr, ignoring descriptors
        '''
        # Guard against infinite loops:
        if self.as_address() in visited:
            return ProxyAlreadyVisited('<...>')
        visited.add(self.as_address())

        attr_dict = {}
        try:
            typeobj = self.type()
            dictoffset = int_from_int(typeobj.field('tp_dictoffset'))
            if dictoffset != 0:
                if dictoffset < 0:
                    type_PyVarObject_ptr = gdb.lookup_type('PyVarObject').pointer()
                    tsize = int_from_int(self._gdbval.cast(type_PyVarObject_ptr)['ob_size'])
                    if tsize < 0:
                        tsize = -tsize
                    size = _PyObject_VAR_SIZE(typeobj, tsize)
                    dictoffset += size
                    assert dictoffset > 0
                    assert dictoffset % SIZEOF_VOID_P == 0

                dictptr = self._gdbval.cast(_type_char_ptr) + dictoffset
                PyObjectPtrPtr = PyObjectPtr.get_gdb_type().pointer()
                dictptr = dictptr.cast(PyObjectPtrPtr)
                attr_dict = PyObjectPtr.from_pyobject_ptr(dictptr.dereference()).proxyval(visited)
        except RuntimeError:
            # Corrupt data somewhere; fail safe
            pass

        tp_name = self.safe_tp_name()

        # New-style class:
        return InstanceProxy(tp_name, attr_dict, long(self._gdbval))

class ProxyException(Exception):
    def __init__(self, tp_name, args):
        self.tp_name = tp_name
        self.args = args

    def __repr__(self):
        return '%s%r' % (self.tp_name, self.args)

class PyBaseExceptionObjectPtr(PyObjectPtr):
    """
    Class wrapping a gdb.Value that's a PyBaseExceptionObject* i.e. an exception
    within the process being debugged.
    """
    _typename = 'PyBaseExceptionObject'

    def proxyval(self, visited):
        # Guard against infinite loops:
        if self.as_address() in visited:
            return ProxyAlreadyVisited('(...)')
        visited.add(self.as_address())
        arg_proxy = PyObjectPtr.from_pyobject_ptr(self.field('args')).proxyval(visited)
        return ProxyException(self.safe_tp_name(),
                              arg_proxy)

class PyBoolObjectPtr(PyObjectPtr):
    """
    Class wrapping a gdb.Value that's a PyBoolObject* i.e. one of the two
    <bool> instances (Py_True/Py_False) within the process being debugged.
    """
    _typename = 'PyBoolObject'

    def proxyval(self, visited):
        if int_from_int(self.field('ob_ival')):
            return True
        else:
            return False


class PyClassObjectPtr(PyObjectPtr):
    """
    Class wrapping a gdb.Value that's a PyClassObject* i.e. a <classobj>
    instance within the process being debugged.
    """
    _typename = 'PyClassObject'


class PyCodeObjectPtr(PyObjectPtr):
    """
    Class wrapping a gdb.Value that's a PyCodeObject* i.e. a <code> instance
    within the process being debugged.
    """
    _typename = 'PyCodeObject'

    def addr2line(self, addrq):
        '''
        Get the line number for a given bytecode offset

        Analogous to PyCode_Addr2Line; translated from pseudocode in
        Objects/lnotab_notes.txt
        '''
        co_lnotab = PyObjectPtr.from_pyobject_ptr(self.field('co_lnotab')).proxyval(set())

        # Initialize lineno to co_firstlineno as per PyCode_Addr2Line
        # not 0, as lnotab_notes.txt has it:
	lineno = int_from_int(self.field('co_firstlineno'))

        addr = 0
        for addr_incr, line_incr in zip(co_lnotab[::2], co_lnotab[1::2]):
            addr += ord(addr_incr)
            if addr > addrq:
                return lineno
            lineno += ord(line_incr)
        return lineno

class PyDictObjectPtr(PyObjectPtr):
    """
    Class wrapping a gdb.Value that's a PyDictObject* i.e. a dict instance
    within the process being debugged.
    """
    _typename = 'PyDictObject'

    def proxyval(self, visited):
        # Guard against infinite loops:
        if self.as_address() in visited:
            return ProxyAlreadyVisited('{...}')
        visited.add(self.as_address())

        result = {}
        for i in safe_range(self.field('ma_mask') + 1):
            ep = self.field('ma_table') + i
            pvalue = PyObjectPtr.from_pyobject_ptr(ep['me_value'])
            if not pvalue.is_null():
                pkey = PyObjectPtr.from_pyobject_ptr(ep['me_key'])
                result[pkey.proxyval(visited)] = pvalue.proxyval(visited)
        return result


class PyInstanceObjectPtr(PyObjectPtr):
    _typename = 'PyInstanceObject'

    def proxyval(self, visited):
        # Guard against infinite loops:
        if self.as_address() in visited:
            return ProxyAlreadyVisited('<...>')
        visited.add(self.as_address())

        # Get name of class:
        in_class = PyObjectPtr.from_pyobject_ptr(self.field('in_class'))
        cl_name = PyObjectPtr.from_pyobject_ptr(in_class.field('cl_name')).proxyval(visited)

        # Get dictionary of instance attributes:
        in_dict = PyObjectPtr.from_pyobject_ptr(self.field('in_dict')).proxyval(visited)

        # Old-style class:
        return InstanceProxy(cl_name, in_dict, long(self._gdbval))


class PyIntObjectPtr(PyObjectPtr):
    _typename = 'PyIntObject'

    def proxyval(self, visited):
        result = int_from_int(self.field('ob_ival'))
        return result

class PyListObjectPtr(PyObjectPtr):
    _typename = 'PyListObject'

    def __getitem__(self, i):
        # Get the gdb.Value for the (PyObject*) with the given index:
        field_ob_item = self.field('ob_item')
        return field_ob_item[i]

    def proxyval(self, visited):
        # Guard against infinite loops:
        if self.as_address() in visited:
            return ProxyAlreadyVisited('[...]')
        visited.add(self.as_address())
        
        result = [PyObjectPtr.from_pyobject_ptr(self[i]).proxyval(visited)
                  for i in safe_range(int_from_int(self.field('ob_size')))]
        return result


class PyLongObjectPtr(PyObjectPtr):
    _typename = 'PyLongObject'

    def proxyval(self, visited):
        '''
        Python's Include/longobjrep.h has this declaration:
           struct _longobject {
               PyObject_VAR_HEAD
               digit ob_digit[1];
           };

        with this description:
            The absolute value of a number is equal to
                 SUM(for i=0 through abs(ob_size)-1) ob_digit[i] * 2**(SHIFT*i)
            Negative numbers are represented with ob_size < 0;
            zero is represented by ob_size == 0.

        where SHIFT can be either:
            #define PyLong_SHIFT        30
            #define PyLong_SHIFT        15
        '''
        ob_size = long(self.field('ob_size'))
        if ob_size == 0:
            return 0L

        ob_digit = self.field('ob_digit')

        if gdb.lookup_type('digit').sizeof == 2:
            SHIFT = 15L
        else:
            # FIXME: I haven't yet tested this case
            SHIFT = 30L

        digits = [long(ob_digit[i]) * 2**(SHIFT*i)
                  for i in safe_range(abs(ob_size))]
        result = sum(digits)
        if ob_size < 0:
            result = -result
        return result


class PyNoneStructPtr(PyObjectPtr):
    """
    Class wrapping a gdb.Value that's a PyObject* pointing to the
    singleton (we hope) _Py_NoneStruct with ob_type PyNone_Type
    """
    _typename = 'PyObject'

    def proxyval(self, visited):
        return None


class PyFrameObjectPtr(PyObjectPtr):
    _typename = 'PyFrameObject'

    def __str__(self):
        fi = FrameInfo(self)
        return str(fi)


class PySetObjectPtr(PyObjectPtr):
    _typename = 'PySetObject'

    def proxyval(self, visited):
        # Guard against infinite loops:
        if self.as_address() in visited:
            return ProxyAlreadyVisited('%s(...)' % self.safe_tp_name())
        visited.add(self.as_address())

        members = []
        table = self.field('table')
        for i in safe_range(self.field('mask')+1):
            setentry = table[i]
            key = setentry['key']
            if key != 0:
                key_proxy = PyObjectPtr.from_pyobject_ptr(key).proxyval(visited)
                if key_proxy != '<dummy key>':
                    members.append(key_proxy)
        if self.safe_tp_name() == 'frozenset':
            return frozenset(members)
        else:
            return set(members)

class PyStringObjectPtr(PyObjectPtr):
    _typename = 'PyStringObject'

    def __str__(self):
        field_ob_size = self.field('ob_size')
        field_ob_sval = self.field('ob_sval')
        char_ptr = field_ob_sval.address.cast(_type_unsigned_char_ptr)
        return ''.join([chr(char_ptr[i]) for i in safe_range(field_ob_size)])

    def proxyval(self, visited):
        return str(self)


class PyTupleObjectPtr(PyObjectPtr):
    _typename = 'PyTupleObject'

    def __getitem__(self, i):
        # Get the gdb.Value for the (PyObject*) with the given index:
        field_ob_item = self.field('ob_item')
        return field_ob_item[i]

    def proxyval(self, visited):
        # Guard against infinite loops:
        if self.as_address() in visited:
            return ProxyAlreadyVisited('(...)')
        visited.add(self.as_address())

        result = tuple([PyObjectPtr.from_pyobject_ptr(self[i]).proxyval(visited)
                        for i in safe_range(int_from_int(self.field('ob_size')))])
        return result


class PyTypeObjectPtr(PyObjectPtr):
    _typename = 'PyTypeObject'


class PyUnicodeObjectPtr(PyObjectPtr):
    _typename = 'PyUnicodeObject'

    def proxyval(self, visited):
        # From unicodeobject.h:
        #     Py_ssize_t length;  /* Length of raw Unicode data in buffer */
        #     Py_UNICODE *str;    /* Raw Unicode buffer */
        field_length = long(self.field('length'))
        field_str = self.field('str')

        # Gather a list of ints from the Py_UNICODE array; these are either
        # UCS-2 or UCS-4 code points:
        Py_UNICODEs = [int(field_str[i]) for i in safe_range(field_length)]

        # Convert the int code points to unicode characters, and generate a
        # local unicode instance:
        result = u''.join([unichr(ucs) for ucs in Py_UNICODEs])
        return result


def int_from_int(gdbval):
    return int(str(gdbval))


def stringify(val):
    # TODO: repr() puts everything on one line; pformat can be nicer, but
    # can lead to v.long results; this function isolates the choice
    if True:
        return repr(val)
    else:
        from pprint import pformat
        return pformat(val)


class FrameInfo:
    '''
    Class representing all of the information we can scrape about a
    PyFrameObject*
    '''
    def __init__(self, fval):
        self.fval = fval
        self.co = PyCodeObjectPtr.from_pyobject_ptr(fval.field('f_code'))
        self.co_name = PyObjectPtr.from_pyobject_ptr(self.co.field('co_name'))
        self.co_filename = PyObjectPtr.from_pyobject_ptr(self.co.field('co_filename'))
        self.f_lineno = int_from_int(fval.field('f_lineno'))
        self.f_lasti = int_from_int(fval.field('f_lasti'))
        self.co_nlocals = int_from_int(self.co.field('co_nlocals'))
        self.co_varnames = PyTupleObjectPtr.from_pyobject_ptr(self.co.field('co_varnames'))
        self.locals = [] # list of kv pairs
        f_localsplus = self.fval.field('f_localsplus')
        for i in safe_range(self.co_nlocals):
            #print 'i=%i' % i
            value = PyObjectPtr.from_pyobject_ptr(f_localsplus[i])
            if not value.is_null():
                name = PyObjectPtr.from_pyobject_ptr(self.co_varnames[i])
                #print 'name=%s' % name
                value = value.proxyval(set())
                #print 'value=%s' % value
                self.locals.append((str(name), value))

    def filename(self):
        '''Get the path of the current Python source file, as a string'''
        return self.co_filename.proxyval(set())

    def current_line_num(self):
        '''Get current line number as an integer (1-based)
        
        Translated from PyFrame_GetLineNumber and PyCode_Addr2Line
        
        See Objects/lnotab_notes.txt
        '''
        f_trace = self.fval.field('f_trace')
        if long(f_trace) != 0:
            # we have a non-NULL f_trace:
            return self.f_lineno
        else:
            #try:
            return self.co.addr2line(self.f_lasti)
            #except ValueError:
            #    return self.f_lineno

    def current_line(self):
        '''Get the text of the current source line as a string, with a trailing
        newline character'''
        with open(self.filename(), 'r') as f:
            all_lines = f.readlines()
            # Convert from 1-based current_line_num to 0-based list offset:
            return all_lines[self.current_line_num()-1]

    def __str__(self):
        return ('Frame 0x%x, for file %s, line %i, in %s (%s)'
                % (long(self.fval._gdbval),
                   self.co_filename,
                   self.current_line_num(),
                   self.co_name,
                   ', '.join(['%s=%s' % (k, stringify(v)) for k, v in self.locals]))
                )


class PyObjectPtrPrinter:
    "Prints a (PyObject*)"

    def __init__ (self, gdbval):
        self.gdbval = gdbval

    def to_string (self):
        proxyval = PyObjectPtr.from_pyobject_ptr(self.gdbval).proxyval(set())
        return stringify(proxyval)


class PyFrameObjectPtrPrinter(PyObjectPtrPrinter):
    "Prints a (PyFrameObject*)"

    def to_string (self):
        pyop = PyObjectPtr.from_pyobject_ptr(self.gdbval)
        fi = FrameInfo(pyop)
        return str(fi)


def pretty_printer_lookup(gdbval):
    type = gdbval.type.unqualified()
    if type.code == gdb.TYPE_CODE_PTR:
        type = type.target().unqualified()
        t = str(type)
        if t == "PyObject":
            return PyObjectPtrPrinter(gdbval)
        elif t == "PyFrameObject":
            return PyFrameObjectPtrPrinter(gdbval)


"""
During development, I've been manually invoking the code in this way:
(gdb) python

import sys
sys.path.append('/home/david/coding/python-gdb')
import libpython
end

then reloading it after each edit like this:
(gdb) python reload(libpython)

The following code should ensure that the prettyprinter is registered
if the code is autoloaded by gdb when visiting libpython.so, provided
that this python file is installed to the same path as the library (or its
.debug file) plus a "-gdb.py" suffix, e.g:
  /usr/lib/libpython2.6.so.1.0-gdb.py
  /usr/lib/debug/usr/lib/libpython2.6.so.1.0.debug-gdb.py
"""
def register (obj):
    if obj == None:
        obj = gdb

    # Wire up the pretty-printer
    obj.pretty_printers.append(pretty_printer_lookup)

register (gdb.current_objfile ())

def get_python_frame(gdb_frame):
    try:
        f = gdb_frame.read_var('f')
        return PyFrameObjectPtr.from_pyobject_ptr(f)
    except ValueError:
        return None

def get_selected_python_frame():
    '''Try to obtain a (gdbframe, PyFrameObjectPtr) pair for the
    currently-running python code, or (None, None)'''
    gdb_frame = gdb.selected_frame()
    while gdb_frame:
        if (gdb_frame.function() is None or
            gdb_frame.function().name != 'PyEval_EvalFrameEx'):
            gdb_frame = gdb_frame.older()
            continue

        try:
            f = gdb_frame.read_var('f')
            return gdb_frame, PyFrameObjectPtr.from_pyobject_ptr(f)
        except ValueError:
            gdb_frame = gdb_frame.older()
    return None, None

class PyList(gdb.Command):
    '''List the current Python source code, if any

    Use
       py-list START
    to list at a different line number within the python source.
    
    Use
       py-list START, END
    to list a specific range of lines within the python source.
    '''

    def __init__(self):
        gdb.Command.__init__ (self,
                              "py-list",
                              gdb.COMMAND_FILES,
                              gdb.COMPLETE_NONE)


    def invoke(self, args, from_tty):
        import re

        start = None
        end = None

        m = re.match(r'\s*(\d+)\s*', args)
        if m:
            start = int(m.group(0))
            end = start + 10

        m = re.match(r'\s*(\d+)\s*,\s*(\d+)\s*', args)
        if m:
            start, end = map(int, m.groups())

        gdb_frame, py_frame = get_selected_python_frame()
        if not py_frame:
            print 'Unable to locate python frame'
            return

        fi = FrameInfo(py_frame)
        filename = fi.filename()
        lineno = fi.current_line_num()

        if start is None:
            start = lineno - 5
            end = lineno + 5

        if start<1:
            start = 1

        with open(filename, 'r') as f:
            all_lines = f.readlines()
            # start and end are 1-based, all_lines is 0-based;
            # so [start-1:end] as a python slice gives us [start, end] as a
            # closed interval
            for i, line in enumerate(all_lines[start-1:end]):
                sys.stdout.write('%4s    %s' % (i+start, line))
            
        
# ...and register the command:
PyList()

def move_in_stack(move_up):
    '''Move up or down the stack (for the py-up/py-down command)'''
    gdb_frame, py_frame = get_selected_python_frame()
    while gdb_frame:
        if move_up:
            iter_frame = gdb_frame.older()
        else:
            iter_frame = gdb_frame.newer()

        if not iter_frame:
            break

        if (iter_frame.function() and 
            iter_frame.function().name == 'PyEval_EvalFrameEx'):
            # Result:
            iter_frame.select()
            py_frame = get_python_frame(iter_frame)
            fi = FrameInfo(py_frame)
            print fi
            sys.stdout.write(fi.current_line())
            return

        gdb_frame = iter_frame

    if move_up:
        print 'Unable to find an older python frame'
    else:
        print 'Unable to find a newer python frame'

class PyUp(gdb.Command):
    'Select and print the python stack frame that called this one (if any)'
    def __init__(self):
        gdb.Command.__init__ (self,
                              "py-up",
                              gdb.COMMAND_STACK,
                              gdb.COMPLETE_NONE)


    def invoke(self, args, from_tty):
        move_in_stack(move_up=True)

PyUp()

class PyDown(gdb.Command):
    'Select and print the python stack frame called by this one (if any)'
    def __init__(self):
        gdb.Command.__init__ (self,
                              "py-down",
                              gdb.COMMAND_STACK,
                              gdb.COMPLETE_NONE)


    def invoke(self, args, from_tty):
        move_in_stack(move_up=False)

PyDown()

class PyBacktrace(gdb.Command):
    'Display the current python frame and all the frames within its call stack (if any)'
    def __init__(self):
        gdb.Command.__init__ (self,
                              "py-bt",
                              gdb.COMMAND_STACK,
                              gdb.COMPLETE_NONE)


    def invoke(self, args, from_tty):
        gdb_frame, py_frame = get_selected_python_frame()
        while gdb_frame:
            gdb_frame = gdb_frame.older()

            if not gdb_frame:
                break

            if (gdb_frame.function() and 
                gdb_frame.function().name == 'PyEval_EvalFrameEx'):
                py_frame = get_python_frame(gdb_frame)
                fi = FrameInfo(py_frame)
                print '  ', fi
                sys.stdout.write(fi.current_line())

PyBacktrace()