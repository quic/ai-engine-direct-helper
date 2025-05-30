#==============================================================================
#
#  Copyright (c) 2025 Qualcomm Technologies, Inc.
#  All Rights Reserved.
#  Confidential and Proprietary - Qualcomm Technologies, Inc.
#
#==============================================================================


Function Download_ai_helper {
    param()
    process {

        $userProfilePath = $env:USERPROFILE

        $ai_helper_url="https://codeload.github.com/quic/ai-engine-direct-helper/zip/refs/heads/main"
        $ai_helper_zip="ai_helper.zip"

        $ai_helper_path="ai_helper"

        $ai_helper_dest_path = "$userProfilePath\code\qualcomm\ai-engine-direct-helper\"
        $ai_helper_cache_path = "$userProfilePath\.cache\qualcomm\$ai_helper_zip"

        if(Test-Path -Path $ai_helper_cache_path)
        {
        }
        else
        {

            Invoke-WebRequest -Uri $ai_helper_url -OutFile $ai_helper_cache_path 
        }

        Expand-Archive -Path $ai_helper_cache_path -DestinationPath $ai_helper_path -Force -ErrorAction Stop
        
        Write-Output ".\$ai_helper_path\ai-engine-direct-helper-main"
        Write-Output $ai_helper_dest_path

        Move-Item -Path ".\$ai_helper_path\ai-engine-direct-helper-main" -Destination $ai_helper_dest_path -Force


    }
}

#Download_ai_helper