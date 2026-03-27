/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import axios from "axios";
import { message } from "antd";

const instance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || '',
  timeout: 30000,
});

instance.interceptors.request.use(
  (config) => {
    return config;
  },
  (err) => {
    message.destroy();
    return Promise.reject(err);
  }
);

instance.interceptors.response.use(
  (response) => {
    if (response.status === 200 && response.data) {
      return response.data;
    } else {
      if (response.data?.message) {
        message.error(response.data.message);
      }
      return Promise.reject(new Error(response.data?.message || 'Request failed'));
    }
  },
  (err) => {
    if (err?.response?.data?.message) {
      message.error(err?.response?.data?.message);
    }
    const origin = window.location && window.location.origin ? window.location.origin : '';
    if (err?.response?.status === 401) {
      const { pathname } = window.location;
      if (pathname !== "/login") {
        window.location.href = `${origin}/login`;
      }
    }
    if (err?.response?.status === 500) {
      window.location.href = `${origin}/500`;
    }

    return Promise.reject(err);
  }
);

const callapi = (method = "GET", url, data = {}, timeout = null) => {
  const config = {
    method,
    url,
    params: method === "GET" ? data : {},
    data: (method === "POST" || method === "PUT") ? data : {},
  };

  if (timeout !== null) {
    config.timeout = timeout;
  }

  return instance(config);
};

export const getApi = (url, data, timeout = null) => callapi("GET", url, data, timeout);
export const postApi = (url, data, timeout = null) => callapi("POST", url, data, timeout);
export const putApi = (url, data, timeout = null) => callapi("PUT", url, data, timeout);
export const deleteApi = (url, timeout = null) => callapi("DELETE", url, {}, timeout);
