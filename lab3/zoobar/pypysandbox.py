import os, sys, errno
from cStringIO import StringIO
from debug import *

pypy_sandbox_dir = '/zoobar/pypy-sandbox'
sys.path = [pypy_sandbox_dir] + sys.path

from pypy.translator.sandbox import pypy_interact, sandlib, vfs
from pypy.translator.sandbox.vfs import Dir, RealDir, RealFile
from pypy.rpython.module.ll_os_stat import s_StatResult
from pypy.tool.lib_pypy import LIB_ROOT

import zoodb
import sqlalchemy
from unixclient import call
import json

class WritableFile(vfs.RealFile):
    def __init__(self, basenode):
        self.path = basenode.path
    def open(self):
        try:
            log(self.path)
            return open(self.path, 'wb')
        except IOError, e:
            raise OSError(e.errno, 'write open failed')

class MySandboxedProc(pypy_interact.PyPySandboxedProc):
    def __init__(self, profile_owner, code, args):
        log("<<<" + profile_owner)
        if not profile_owner.isalnum():
            raise ValueError("unsafe profile_owner name")
        self.profile_owner = profile_owner
        super(MySandboxedProc, self).__init__(
            pypy_sandbox_dir + '/pypy/translator/goal/pypy-c',
            ['-S', '-c', code] + args
        )
        self.debug = True
        self.virtual_cwd = '/'

    ## Replacements for superclass functions
    def get_node(self, vpath):
        dirnode, name = self.translate_path(vpath)
        log(dirnode)
        if name:
            node = dirnode.join(name)
        else:
            node = dirnode
        if self.debug:
            sandlib.log.vpath('%r => %r' % (vpath, node))
        return node

    def handle_message(self, fnname, *args):
        if '__' in fnname:
            raise ValueError("unsafe fnname")
        try:
            handler = getattr(self, 'do_' + fnname.replace('.', '__'))
        except AttributeError:
            raise RuntimeError("no handler for " + fnname)
        resulttype = getattr(handler, 'resulttype', None)
        return handler(*args), resulttype

    def build_virtual_root(self):
        # build a virtual file system:
        # * can access its own executable
        # * can access the pure Python libraries
        # * can access the temporary usession directory as /tmp
        exclude = ['.pyc', '.pyo']
        #tmpdirnode = RealDir('/tmp/sandbox-root', exclude=exclude)
        dirname = '/tmp/sandbox-root-' + self.profile_owner
        tmpdirnode = RealDir(dirname, exclude=exclude)
        libroot = str(LIB_ROOT)
        log(libroot)
        return Dir({
            'proflib.py': RealFile('zoobar/proflib.py'),
            'bin':  Dir({'pypy-c':     RealFile(self.executable),
                         'lib-python': RealDir(libroot + '/lib-python', exclude=exclude),
                         'lib_pypy':   RealDir(libroot + '/lib_pypy',   exclude=exclude),
#                         'lib_zoobar': RealDir('/zoobar', exclude=exclude)
                        }),
            'proc': Dir({'cpuinfo': RealFile('/proc/cpuinfo'), }),
            'tmp':  tmpdirnode,
            })

    ## Implement / override system calls
    ##
    ## Useful reference:
    ##    pypy-sandbox/pypy/translator/sandbox/sandlib.py
    ##    pypy-sandbox/pypy/translator/sandbox/vfs.py
    ##
    def do_ll_os__ll_os_geteuid(self):
        return 0

    def do_ll_os__ll_os_getuid(self):
        return 0

    def do_ll_os__ll_os_getegid(self):
        return 0

    def do_ll_os__ll_os_getgid(self):
        return 0

    def do_ll_os__ll_os_fstat(self, fd):
        ## Limitation: fd's 0, 1, and 2 are not in open_fds table
        f = self.get_file(fd)
        try:
            return os.fstat(f.fileno())
        except:
            raise OSError(errno.EINVAL)
    do_ll_os__ll_os_fstat.resulttype = s_StatResult

    def do_ll_os__ll_os_open(self, vpathname, flags, mode):
        if vpathname.find("xfers#") != -1:
            self.username = vpathname.split('#')[1].strip()
            log(vpathname)
            return 0
        elif vpathname.find("user#") != -1:
            log(vpathname)
            self.username = vpathname.split('#')[1].strip()
            return 0
        elif vpathname.find("xfer#") != -1:
            log(vpathname)
            self.rcptname = vpathname.split('#')[1].strip()
            self.zoobars = int(vpathname.split('#')[2].strip())
            self.selfname = vpathname.split('#')[3].strip()
            self.token = vpathname.split('#')[4].strip()
            return 0

        if flags & (os.O_CREAT):
            dirnode, name = self.translate_path(vpathname)
            ## LAB 3: handle file creation
            log(dirnode.path + '/' + name)
            if not os.path.exists(dirnode.path):
                os.mkdir(dirnode.path)
            #fd = os.open(dirnode.path + '/' + name, \
                    #os.O_CREAT | os.O_WRONLY | os.O_APPEND)
            fd = open(dirnode.path + '/' + name, 'w')
            log(fd)
        node = self.get_node(vpathname)
        if flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR) != os.O_RDONLY:
            log(flags)
            ## LAB 3: handle writable files, by not raising OSError in some cases
            if flags & (os.O_RDWR | os.O_WRONLY):
                node = WritableFile(node)
            else:
                raise OSError(errno.EPERM, "write access denied")
        f = node.open()
        return self.allocate_fd(f)

    def do_ll_os__ll_os_write(self, fd, data):
        try:
            f = self.get_file(fd)
        except:
            f = None

        if f is not None:
            ## LAB 3: if this file should be writable, do the write,
            ##        and return the number of bytes written
            f.write(data)
            return len(data)
            #raise OSError(errno.EPERM, "write not implemented yet")

        return super(MySandboxedProc, self).do_ll_os__ll_os_write(fd, data)

    def do_ll_os__ll_os_read(self, fd, size):
        if size >= 0:
            return super(MySandboxedProc, self).do_ll_os__ll_os_read(fd, size)

        if size == -1:
            log(self.username)
            ret = self.pypy_get_xfers(self.username)
        elif size == -2:
            log(self.username)
            ret = self.pypy_get_user(self.username)
        elif size == -3:
            self.pypy_xfer(self.rcptname, \
                    self.zoobars, \
                    self.selfname,
                    self.token)
            return None
        return json.dumps(ret)

    def pypy_get_xfers(self, username):
        log(username)
        xfer_db = zoodb.transfer_setup()
        xfers = []
        for xfer in xfer_db.query(zoodb.Transfer).filter(
                        sqlalchemy.or_(zoodb.Transfer.sender==username,
                                       zoodb.Transfer.recipient==username)):
            xfers += [{'sender': xfer.sender,
                       'recipient': xfer.recipient,
                       'amount': xfer.amount,
                       'time': xfer.time}]
        return xfers

    def pypy_get_user(self, username):
        log(username)
        person_db = zoodb.person_setup()
        balance_db = zoodb.balance_setup()
        p = person_db.query(zoodb.Person).get(username)
        balance = balance_db.query(zoodb.Balance).get(username)
        log("user is: %s zoobars is: %d"%(username, balance.zoobars))
        if not p:
            return None
        return {'username': p.username,
                'profile': p.profile,
                'zoobars': balance.zoobars}

    def pypy_xfer(self, rcptname, zoobars, selfname, token):
        balance_db = zoodb.balance_setup()
        sender = balance_db.query(zoodb.Balance).get(selfname)
        recipient = balance_db.query(zoodb.Balance).get(rcptname)

        if not sender:
            raise Exception('sender ' + selfname + ' not found')
        if not recipient:
            raise Exception('recipient ' + rcptname + ' not found')

        sender_balance = sender.zoobars - zoobars
        recipient_balance = recipient.zoobars + zoobars

        if sender_balance < 0 or recipient_balance < 0:
            raise ValueError()
        
        msg = 'modify@#' \
            + selfname + "@#" \
            + str(sender_balance) + "@#" \
            + token
        resp = call("blnssvc/sock", msg).strip()
        if not resp:
            raise ValueError()

        msg = 'modify@#' \
            + rcptname + "@#" \
            + str(recipient_balance) + "@#" \
            + token
        resp = call("blnssvc/sock", msg).strip()
            
        msg = selfname + "@#" \
            + rcptname + "@#" \
            + str(zoobars)

        resp = call("logsvc/sock", msg).strip()


def run(profile_owner, code, args = [], timeout = None):
    sandproc = MySandboxedProc(profile_owner, code, args)
    if timeout is not None:
        sandproc.settimeout(timeout, interrupt_main=True)
    try:
        code_output = StringIO()
        log(code_output.getvalue())
        sandproc.interact(stdout=code_output, stderr=code_output)
        log(code_output.getvalue())
        return code_output.getvalue()
    finally:
        sandproc.kill()

