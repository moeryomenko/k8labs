/** @file
  Enroll the lab Secure Boot trust anchors at every boot.

  The Cloud Hypervisor firmware is opened read-only by the VMM and its variable
  store is emulated, so keys enrolled once do not survive a reboot. This driver
  re-creates PK/KEK/db from the default-key FFS files embedded in the firmware
  and enrolls them, which turns Secure Boot on for the boot that follows.

  SPDX-License-Identifier: BSD-2-Clause-Patent
**/

#include <Uefi.h>

#include <Library/DebugLib.h>
#include <Library/SecureBootVariableProvisionLib.h>
#include <Library/UefiDriverEntryPoint.h>

STATIC
VOID
LogStatus (
  IN CONST CHAR8  *What,
  IN EFI_STATUS    Status
  )
{
  if (EFI_ERROR (Status)) {
    DEBUG ((DEBUG_WARN, "K8LabsEnrollKeys: %a: %r\n", What, Status));
  }
}

/**
  Entry point: seed the *Default variables from FFS, then enroll them.

  @param[in] ImageHandle  Image handle of this driver.
  @param[in] SystemTable  Pointer to the EFI System Table.

  @retval EFI_SUCCESS  Enrollment completed or was already in place.
  @retval Others       A default-key file is missing from the firmware.
**/
EFI_STATUS
EFIAPI
K8LabsEnrollKeysEntryPoint (
  IN EFI_HANDLE        ImageHandle,
  IN EFI_SYSTEM_TABLE  *SystemTable
  )
{
  EFI_STATUS  Status;

  Status = SecureBootInitPKDefault ();
  if (EFI_ERROR (Status) && (Status != EFI_UNSUPPORTED)) {
    DEBUG ((DEBUG_ERROR, "K8LabsEnrollKeys: PKDefault missing: %r\n", Status));
    return Status;
  }

  Status = SecureBootInitKEKDefault ();
  if (EFI_ERROR (Status) && (Status != EFI_UNSUPPORTED)) {
    DEBUG ((DEBUG_ERROR, "K8LabsEnrollKeys: KEKDefault missing: %r\n", Status));
    return Status;
  }

  Status = SecureBootInitDbDefault ();
  if (EFI_ERROR (Status) && (Status != EFI_UNSUPPORTED)) {
    DEBUG ((DEBUG_ERROR, "K8LabsEnrollKeys: dbDefault missing: %r\n", Status));
    return Status;
  }

  //
  // Order matters: PK establishes platform ownership, KEK and db follow it.
  // Every call is best effort: an already-enrolled variable is not an error.
  //
  LogStatus ("enroll PK", EnrollPKFromDefault ());
  LogStatus ("enroll KEK", EnrollKEKFromDefault ());
  LogStatus ("enroll db", EnrollDbFromDefault ());

  return EFI_SUCCESS;
}
