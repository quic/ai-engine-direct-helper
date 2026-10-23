//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef GENIEAPISERVICE_H
#define GENIEAPISERVICE_H

#include <httplib.h>
#include <memory>
#include <atomic>

class ModelManager;
class ChatRequestHandler;

class GenieService
{
public:
    void run(int argc, char *argv[]);

    void ServiceStop();

    static void TriggerGracefulShutdown(int exit_code);

    class Route;

private:
    std::atomic<bool> init_{false};
    std::atomic<bool> shutdown_requested_{false};
    std::atomic<bool> shutdown_completed_{false};
    std::atomic<bool> stop_in_progress_{false};
    std::unique_ptr<ModelManager> modelManager;
    httplib::Server svr;
    std::unique_ptr<ChatRequestHandler> requestHandler;
    static inline GenieService *self_;

    void setupSignalHandlers();

    void setupHttpServer();

    friend ChatRequestHandler;
    std::vector<std::shared_ptr<Route> > routes_{};
};

#endif //GENIEAPISERVICE_H
