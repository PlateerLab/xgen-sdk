from __future__ import print_function

from SDBAPIForPythonFile import SDBAPI
from pathlib import Path

import shutil


def main():
    api = SDBAPI()
    
    policyId = "ARIA_256_UDE"
    
    # Encrypt target file
    plainFile = "/root/Testing/sample/origin/test.txt"
    # Encrypt result file & Decrypt target file
    encFile = "/root/Testing/sample/encrypt/test.txt.enc"
    # Decrypt result file
    decFile = "/root/Testing/sample/decrypt/test.txt.dec"
    # Encrypted Backup file
    encFileBackup = "/root/Testing/sample/encrypt/test.txt.enc.bak"

    # Plain file is not exists or Plain backup file is exists
    if not Path(plainFile).exists():
        raise FileNotFoundError(
                f"Plain file does not exist: {Path(plainFile).resolve()}\n"
                "Please create test.txt file."
                )
    elif Path(plainFile + ".bak").exists():
        raise FileExistsError(
                f"Plain backup file already exists: {Path(plainFile + '.bak').resolve()}\n"
                "Please remove test.txt.bak file."
                )

    # Encrypted backup file or Decrypted file is already exists
    if Path(encFileBackup).exists():
        raise FileExistsError(
                f"Encrypted backup file already exists: {Path(encFileBackup).resolve()}\n"
                "Please remove the encrypted backup file."
                )
    if Path(decFile).exists():
        raise FileExistsError(
                f"Decrypted file already exists: {Path(decFile).resolve()}\n"
                "Please remove the decrypted file."
                )

    # encResult & decResult
    # Success == 1
    # Fail == 0 or error
    encResult = api.EncryptFile(policyId, plainFile, encFile)
    if encResult == 0:
        print("Encrypt failed:", api.GetLastErrorMsg())
        return

    # Backup Encrypt file
    shutil.copyfile(encFile, encFileBackup)

    decResult = api.DecryptFile(policyId, encFile, decFile)
    if decResult == 0:
        print("Decrypt failed:", api.GetLastErrorMsg())
        return


    # Printing plainFile, encFile, decFile data
    plainFile = plainFile + ".bak"
    print(f"Used Policy Name: {policyId}")
    print("=" * 60)
    print(f"Plain File Path: {plainFile}")
    print("Plain File Data")
    with open(plainFile, "r", encoding="utf-8", errors="replace") as f:
        print(f.read())
    print("=" * 60)
    print(f"EncryptFile result: {encResult}")
    print(f"Encrypted File Path: {encFileBackup}")
    print("Encrypt File Data")
    with open(encFileBackup, "r", encoding="utf-8", errors="replace") as f:
        print(f.read())
    print("=" * 60)
    print(f"DecryptFile result: {decResult}")
    print(f"Decrypted File Path: {decFile}")
    print("Decrypt File data")
    with open(decFile, "r", encoding="utf-8", errors="replace") as f:
        print(f.read())


if __name__ == "__main__":
    main()
