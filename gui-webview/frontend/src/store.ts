import { Config, Flavour, Profile } from "./api";
import { readable, writable } from "svelte/store";
import { Api } from "./api";
import { RClient } from "./ipc";

const defaultStorage = [
    {
        addon_dir: '',
        auto_update_check: false,
        config_dir: '',
        game_flavour: Flavour.retail,
        profile: 'Default',
        temp_dir: ''
    }
]

export type KeyedConfig = {
    [key: string]: Config
};
let p = defaultStorage.reduce((acc, cur) => {
    acc[cur.profile] = cur
    return acc
},<KeyedConfig>{})


export const profiles = writable<Map<Profile, Config>>(JSON.parse(localStorage.getItem('instawow')));
export const activeProfile = writable<Profile | undefined>();
export const api = readable(new Api());