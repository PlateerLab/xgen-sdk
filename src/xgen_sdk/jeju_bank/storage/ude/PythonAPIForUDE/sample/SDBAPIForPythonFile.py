import ctypes
import os
import sys
import platform


class SDBAPI:

    def __init__(self, lib_path=None, charset=None):

        self.charset = charset if charset else sys.getdefaultencoding()

        system = platform.system()

        if lib_path:
            load_path = lib_path
        else:
            sdb_home = os.getenv("SDB_HOME")
            if not sdb_home:
                raise EnvironmentError("SDB_HOME environment variable not set")

            if system == "Linux":
                load_path = os.path.join(sdb_home, "lib", "libSDBAPIForUDE.so")
            elif system == "Windows":
                load_path = os.path.join(sdb_home, "lib", "SDBAPIForUDE.dll")
            else:
                raise OSError(f"Unsupported OS: {system}")

        if system == "Windows":
            self.lib = ctypes.WinDLL(load_path)
        else:
            self.lib = ctypes.CDLL(load_path)

        self._declare_functions()

    def _declare_functions(self):

        self.lib.SDB_Init.argtypes = []
        self.lib.SDB_Init.restype = ctypes.c_int

        self.lib.SDB_IsInit.argtypes = []
        self.lib.SDB_IsInit.restype = ctypes.c_int

        self.lib.SDB_InitResult.argtypes = []
        self.lib.SDB_InitResult.restype = ctypes.c_char_p

        self.lib.SDB_checkAgent.argtypes = [ctypes.c_char_p]
        self.lib.SDB_checkAgent.restype = ctypes.c_char_p

        self.lib.SDB_ClearKeys.argtypes = []
        self.lib.SDB_ClearKeys.restype = None

        self.lib.SDB_Encrypt.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_int
        ]

        self.lib.SDB_Encrypt.restype = ctypes.c_void_p

        self.lib.SDB_Decrypt.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_int
        ]

        self.lib.SDB_Decrypt.restype = ctypes.c_void_p
        
# Unstructure Data (File) Encrypt and Decrypt Setting Start
        self.lib.SDB_EncryptFile.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int
        ]

        self.lib.SDB_EncryptFile.restype = ctypes.c_int

        self.lib.SDB_DecryptFile.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int
        ]

        self.lib.SDB_DecryptFile.restype = ctypes.c_int

# Unstructure Data (File) Encrypt and Decrypt Setting End

        self.lib.SDB_SetAgentHome.argtypes = [ctypes.c_char_p]
        self.lib.SDB_SetAgentHome.restype = ctypes.c_int

        self.lib.SDB_SetFirstPort.argtypes = [ctypes.c_ushort]
        self.lib.SDB_SetFirstPort.restype = ctypes.c_int

        self.lib.SDB_SetSecondPort.argtypes = [ctypes.c_ushort]
        self.lib.SDB_SetSecondPort.restype = ctypes.c_int

        self.lib.SDB_SetLogLevel.argtypes = [ctypes.c_char_p]
        self.lib.SDB_SetLogLevel.restype = ctypes.c_int

        self.lib.SDB_SetLogDir.argtypes = [ctypes.c_char_p]
        self.lib.SDB_SetLogDir.restype = ctypes.c_int

        self.lib.SDB_SetFirstIp.argtypes = [ctypes.c_char_p]
        self.lib.SDB_SetFirstIp.restype = ctypes.c_int

        self.lib.SDB_SetSecondIp.argtypes = [ctypes.c_char_p]
        self.lib.SDB_SetSecondIp.restype = ctypes.c_int

        self.lib.SDB_SetUseLog.argtypes = [ctypes.c_int]
        self.lib.SDB_SetUseLog.restype = ctypes.c_int

        self.lib.SDB_GetLastErrorMsg.argtypes = []
        self.lib.SDB_GetLastErrorMsg.restype = ctypes.c_char_p

        self.lib.SDB_GetLastErrorCode.argtypes = []
        self.lib.SDB_GetLastErrorCode.restype = ctypes.c_int

    def setCharset(self, charset):
        self.charset = charset

    def Init(self):
        return self.lib.SDB_Init()

    def IsInit(self):
        return self.lib.SDB_IsInit()

    def InitResult(self):
        r = self.lib.SDB_InitResult()
        return r.decode(self.charset) if r else None

    def checkAgent(self, flag):
        r = self.lib.SDB_checkAgent(flag.encode(self.charset))
        return r.decode(self.charset) if r else None

    def ClearKeys(self):
        self.lib.SDB_ClearKeys()

# Unstructure Data (File) Encrypt and Decrypt Start
    def _raise_if_failed(self, ret):
        if ret == 0:
            return 1

        err = self.lib.SDB_GetLastErrorMsg()
        if err:
            # [xgen 수정 1/1] 원본은 self.encoding 참조 — 이 클래스에 없는 속성이라
            # 실패 경로에서 AttributeError 가 나던 벤더 결함. self.charset 으로 교정.
            message = err.decode(self.charset, errors="replace")
        else:
            message = "unknown error"

        return 0

    def EncryptFile(self, policy, src_path, enc_dst_path):

        if isinstance(src_path, str):
            src_path_b = src_path.encode(self.charset)
        else:
            src_path_b = src_path
        
        if isinstance(enc_dst_path, str):
            enc_dst_path_b = enc_dst_path.encode(self.charset)
        else:
            enc_dst_path_b = enc_dst_path

        ret = self.lib.SDB_EncryptFile(
            policy.encode(self.charset),
            src_path_b,
            enc_dst_path_b,
            0
        )
        return self._raise_if_failed(ret)
        
    def DecryptFile(self, policy, enc_src_path, dec_dst_path):
        
        if isinstance(enc_src_path, str):
            enc_src_path_b = enc_src_path.encode(self.charset)
        else:
            enc_src_path_b = enc_src_path
        
        if isinstance(dec_dst_path, str):
            dec_dst_path_b = dec_dst_path.encode(self.charset)
        else:
            dec_dst_path_b = dec_dst_path

        ret = self.lib.SDB_DecryptFile(
            policy.encode(self.charset),
            enc_src_path_b,
            dec_dst_path_b,
            0
        )
        return self._raise_if_failed(ret)

# Unstructure Data (File) Encrypt and Decrypt End

    def SetAgentHome(self, path):
        return self.lib.SDB_SetAgentHome(path.encode(self.charset))

    def SetFirstPort(self, port):
        return self.lib.SDB_SetFirstPort(int(port))

    def SetSecondPort(self, port):
        return self.lib.SDB_SetSecondPort(int(port))

    def SetLogLevel(self, level):
        return self.lib.SDB_SetLogLevel(level.encode(self.charset))

    def SetLogDir(self, path):
        return self.lib.SDB_SetLogDir(path.encode(self.charset))

    def SetFirstIp(self, ip):
        return self.lib.SDB_SetFirstIp(ip.encode(self.charset))

    def SetSecondIp(self, ip):
        return self.lib.SDB_SetSecondIp(ip.encode(self.charset))

    def SetUseLog(self, use):
        return self.lib.SDB_SetUseLog(int(use))

    def GetLastErrorMsg(self):
        r = self.lib.SDB_GetLastErrorMsg()
        return r.decode(self.charset) if r else None

    def GetLastErrorCode(self):
        return self.lib.SDB_GetLastErrorCode()
